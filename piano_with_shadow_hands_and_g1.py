"""A task where two shadow hands must play a given MIDI file on a piano."""

from typing import List, Optional, Sequence, Tuple

import numpy as np
from scipy.optimize import linear_sum_assignment
from dm_control import mjcf
from dm_control.composer import variation as base_variation
from dm_control.composer.observation import observable
from dm_control.mjcf import commit_defaults
from dm_control.utils.rewards import tolerance
from dm_env import specs
from mujoco_utils import collision_utils, spec_utils

import mink  # Add mink import
import mujoco

import robopianist.models.hands.shadow_hand_constants as hand_consts
from robopianist.models.arenas import stage
from robopianist.music import midi_file
from robopianist.suite import composite_reward
from robopianist.suite.tasks import base
from robopianist.suite.tasks.piano_with_shadow_hands import PianoWithShadowHands
from loop_rate_limiters import RateLimiter

from g1_entity import G1Entity

# Distance thresholds for the shaping reward.
_FINGER_CLOSE_ENOUGH_TO_KEY = 0.01
_KEY_CLOSE_ENOUGH_TO_PRESSED = 0.05

# Energy penalty coefficient.
_ENERGY_PENALTY_COEF = 5e-3

# Transparency of fingertip geoms.
_FINGERTIP_ALPHA = 1.0

# Bounds for the uniform distribution from which initial hand offset is sampled.
_POSITION_OFFSET = 0.05

# Height offset for all components
_HEIGHT_OFFSET = 0.76

# Modified hand positions - raised higher
_LEFT_HAND_POSITION = (0.4, -0.15, 0.13 + _HEIGHT_OFFSET)  # Original z=0.13
_RIGHT_HAND_POSITION = (0.4, 0.15, 0.13 + _HEIGHT_OFFSET)  # Original z=0.13

import os

class PianoWithShadowHandsAndG1(PianoWithShadowHands):
    def __init__(self, *args, **kwargs):
        # Store height offset before calling super().__init__
        self._height_offset = _HEIGHT_OFFSET
        
        # Override the hand positions in the parent class
        import sys
        sys.modules['robopianist.suite.tasks.base']._LEFT_HAND_POSITION = _LEFT_HAND_POSITION
        sys.modules['robopianist.suite.tasks.base']._RIGHT_HAND_POSITION = _RIGHT_HAND_POSITION
        
        super().__init__(*args, **kwargs)
        self._camera_angle = 0.0
        self._camera_radius = 1.0
        self._camera_height = 1.0
        self._camera_angular_velocity = 0.01
        self._setup_g1_arm_joints()
        self.add_g1()
        self._disable_collisions_between_hands_and_g1()
        self._setup_camera()
        self._raise_piano()
        
        # Add IK rate limiting
        self._ik_update_rate = 5  # Run IK every 5 timesteps
        self._ik_counter = 0
        self._last_velocities = None

    def _euler_to_quat(self, roll, pitch, yaw):
        """Convert euler angles to quaternion."""
        # Convert to radians
        roll, pitch, yaw = np.radians([roll, pitch, yaw])
        
        # Compute quaternion components
        cr, cp, cy = np.cos(roll/2), np.cos(pitch/2), np.cos(yaw/2)
        sr, sp, sy = np.sin(roll/2), np.sin(pitch/2), np.sin(yaw/2)
        
        w = cr * cp * cy + sr * sp * sy
        x = sr * cp * cy - cr * sp * sy
        y = cr * sp * cy + sr * cp * sy
        z = cr * cp * sy - sr * sp * cy
        
        return [w, x, y, z]

    def _setup_camera(self) -> None:
        """Set up the panning camera."""
        self._camera = self._arena.mjcf_model.worldbody.add(
            'camera',
            name='panning_camera',
            pos=[self._camera_radius, 0, self._camera_height],
            quat=self._euler_to_quat(0, 0, 0),  # Point camera horizontally
            mode='fixed'  # Use fixed mode to allow manual control
        )

    def _find_g1_model(self) -> Optional[str]:
        """Find the G1 model XML file.
        
        Returns:
            Path to the G1 model file if found, None otherwise.
        """
        # First try the modified version, then the original
        potential_paths = [
            os.path.join(os.path.dirname(__file__), "mujoco_menagerie/unitree_g1/g1_modified.xml"),
            os.path.expanduser("~/mujoco_menagerie/unitree_g1/g1_modified.xml"),
            "/usr/local/share/mujoco_menagerie/unitree_g1/g1_modified.xml",
            os.path.join(os.path.dirname(__file__), "mujoco_menagerie/unitree_g1/g1.xml"),
            os.path.expanduser("~/mujoco_menagerie/unitree_g1/g1.xml"),
            "/usr/local/share/mujoco_menagerie/unitree_g1/g1.xml",
        ]
        
        for path in potential_paths:
            if os.path.exists(path):
                print(f"Found G1 model at {path}")
                return path
                
        return None

    def add_g1(self):
        """Add Unitree G1 robot to the environment."""
        try:
            # Default position behind the piano
            position = [0.45, 0.0, 0.0]  # x, y, z coordinates
            
            print("\n=== Adding G1 Debug ===")
            
            # Create attachment site with unique name and rotated orientation
            site_name = f'g1_attachment_{id(self)}'  # Use unique identifier
            attachment_site = self._arena.mjcf_model.worldbody.add(
                'site',
                name=site_name,
                size=[0.01, 0.01, 0.01],
                pos=position,
                euler=[0, 0, 3.14159]  # Rotate 180 degrees around Z axis (in radians)
            )
            print(f"Created attachment site with rotation")

            # Find the G1 model path
            model_path = self._find_g1_model()
            if model_path is None:
                raise ValueError("Could not find Unitree G1 model file")

            # Create and attach G1 entity
            g1_entity = G1Entity(model_path)
            self._arena.attach(g1_entity, attachment_site)
            
            # Store G1 entity reference
            self._g1 = g1_entity
            
            print("Successfully added G1 to environment")
            print("=== End Adding G1 Debug ===\n")
            
        except Exception as e:
            print(f"Error adding G1 to environment: {e}")
            import traceback
            traceback.print_exc()
            self._g1 = None

    def _initialize_g1_position(self, physics: mjcf.Physics) -> None:
        """Initialize the G1's position relative to the shadow hands."""
        if not hasattr(self, '_g1') or self._g1 is None:
            return

        try:
            print("\n=== Initializing G1 Position ===")
            
            # Find the G1's root body
            root_name = "g1_29dof_rev_1_0/"
            root_id = physics.model.name2id(root_name, "body")
            
            if root_id >= 0:
                # Position G1 behind and slightly above the piano
                # Account for the height offset
                base_pos = [0.0, 0.0, 0.7 + self._height_offset]
                # base_pos = [0.0, 0.0, 0.0]
                physics.data.xpos[root_id] = base_pos
                
                # Orient G1 to face the piano
                physics.data.xquat[root_id] = [0, 0, 1, 0]  # 180° around Y axis
                
                # Get hand positions for initial arm positioning
                hand_positions = self._get_shadow_hand_positions(physics)
                
                # Update arm positions to match hands
                self._update_g1_arms(physics, hand_positions)
                
                print(f"G1 base position set to: {base_pos}")
                print(f"Initial hand positions: {hand_positions}")
            else:
                print(f"Warning: Could not find G1 root body with name {root_name}")
                
            print("=== End Initializing G1 Position ===\n")
            
        except Exception as e:
            print(f"Error initializing G1 position: {e}")
            import traceback
            traceback.print_exc()

    def initialize_episode(self, physics: mjcf.Physics, random_state: np.random.RandomState) -> None:
        """Initialize episode and raise components."""
        # First call parent's initialize_episode
        super().initialize_episode(physics, random_state)
        
        print("\n=== Raising Hands Debug ===")
        # Raise the hands using physics
        for hand_name, hand in [("right", self.right_hand), ("left", self.left_hand)]:
            hand_body = physics.bind(hand.root_body)
            current_pos = hand_body.xpos.copy()
            new_pos = [current_pos[0], current_pos[1], current_pos[2] + self._height_offset]
            hand_body.xpos = new_pos
            print(f"{hand_name.capitalize()} hand position: {current_pos} -> {new_pos}")
        
        # Apply changes
        physics.forward()
        print("=== End Raising Hands Debug ===\n")
        
        # Initialize G1 position after hands are positioned
        self._initialize_g1_position(physics)

        print("initializing mink")
        # Initialize mink configuration if not already done
        self._initialize_mink(physics)

        print("mink initialized")
        
        # Reset and update camera
        self._camera_angle = 0.0
        camera = physics.bind(self._camera)
        camera.pos = [self._camera_radius, 0, self._camera_height]
        camera.quat = self._euler_to_quat(0, 1.0, 0)

    # TODO: the below functions are from piano with shadow hands. 
    def _set_rewards(self) -> None:
        self._reward_fn = composite_reward.CompositeReward(
            key_press_reward=self._compute_key_press_reward,
            sustain_reward=self._compute_sustain_reward,
            energy_reward=self._compute_energy_reward,
        )
        if not self._disable_fingering_reward:
            self._reward_fn.add("fingering_reward", self._compute_fingering_reward)
        else:
            # use OT based fingering
            print('Fingering is unavailable. OT fingering reward is used.')
            self._reward_fn.add("ot_fingering_reward", self._compute_ot_fingering_reward)

        if not self._disable_forearm_reward:
            self._reward_fn.add("forearm_reward", self._compute_forearm_reward)

    def _reset_quantities_at_episode_init(self) -> None:
        self._t_idx: int = 0
        self._should_terminate: bool = False
        self._discount: float = 1.0

    def _maybe_change_midi(self, random_state: np.random.RandomState) -> None:
        if self._augmentations is not None:
            midi = self._initial_midi
            for var in self._augmentations:
                midi = var(initial_value=midi, random_state=random_state)
            self._midi = midi
            self._reset_trajectory()

    def _reset_trajectory(self) -> None:
        note_traj = midi_file.NoteTrajectory.from_midi(
            self._midi, self.control_timestep
        )
        note_traj.add_initial_buffer_time(self._initial_buffer_time)
        self._notes = note_traj.notes
        self._sustains = note_traj.sustains

    def _get_shadow_hand_positions(self, physics: mjcf.Physics) -> dict:
        """Get the current positions of both shadow hands."""
        positions = {}
        
        # Get palm/forearm positions for both hands using root_body
        positions['left'] = physics.bind(self.left_hand.root_body).xpos.copy()
        positions['right'] = physics.bind(self.right_hand.root_body).xpos.copy()
        
        # Add offset to move target position from forearm to approximate wrist position
        # The shadow hand's forearm is about 0.1m long, so move the target forward
        forearm_to_wrist_offset = np.array([0, 0, 0])
        positions['left'] += forearm_to_wrist_offset
        positions['right'] += forearm_to_wrist_offset

        print(f"positions: {positions}")
        
        return positions

    def _setup_g1_arm_joints(self):
        """Set up the G1 arm joints and IK configuration."""
        # Define joint names for both arms - no prefix, exactly as in XML
        self._left_arm_joints = [
            "left_shoulder_pitch_joint",
            "left_shoulder_roll_joint", 
            "left_shoulder_yaw_joint",
            "left_elbow_joint",
            "left_wrist_roll_joint",
            "left_wrist_pitch_joint",
            "left_wrist_yaw_joint"
        ]
        
        self._right_arm_joints = [
            "right_shoulder_pitch_joint",
            "right_shoulder_roll_joint",
            "right_shoulder_yaw_joint", 
            "right_elbow_joint",
            "right_wrist_roll_joint",
            "right_wrist_pitch_joint",
            "right_wrist_yaw_joint"
        ]
        
        # Define prefix for body names only (bodies do use the prefix)
        prefix = "g1_29dof_rev_1_0/"
        
        # Define body names for both arms (these still need the prefix)
        self._left_arm_bodies = [
            f"{prefix}left_shoulder_pitch_link",
            f"{prefix}left_shoulder_roll_link",
            f"{prefix}left_shoulder_yaw_link", 
            f"{prefix}left_elbow_link",
            f"{prefix}left_wrist_roll_link",
            f"{prefix}left_wrist_pitch_link",
            f"{prefix}left_wrist_yaw_link"
        ]
        
        self._right_arm_bodies = [
            f"{prefix}right_shoulder_pitch_link",
            f"{prefix}right_shoulder_roll_link",
            f"{prefix}right_shoulder_yaw_link",
            f"{prefix}right_elbow_link",
            f"{prefix}right_wrist_roll_link",
            f"{prefix}right_wrist_pitch_link",
            f"{prefix}right_wrist_yaw_link"
        ]

    def _initialize_mink(self, physics: mjcf.Physics):
        """Initialize mink IK solver configuration and tasks."""
        # Create configuration from model
        self._mink_config = mink.Configuration(physics.model.ptr)

        print("mink config created")
        
        # Create tasks for both hands
        self._mink_tasks = []
        
        # Add frame tasks for both hands - right first, then left
        for hand in ["right_wrist_yaw_link", "left_wrist_yaw_link"]:  # Changed order to right first
            task = mink.FrameTask(
                frame_name=f"g1_29dof_rev_1_0/{hand}",
                frame_type="body",
                position_cost=100.0,  # Reduced from 50.0 for smoother motion
                orientation_cost=0.5,  # Reduced from 1.0
                lm_damping=5.0,  # Increased from 1.0 for more stability
            )
            self._mink_tasks.append(task)
        
        # Set limits to None to disable them
        self._mink_limits = None
        print("mink initialization complete")

    def _update_g1_arms(self, physics: mjcf.Physics, hand_positions: dict) -> None:
        """Update G1 arm positions using mink IK solver."""
        try:
            # Initialize mink configuration if not already done
            if not hasattr(self, '_mink_config') or self._mink_config is None:
                self._initialize_mink(physics)
            
            try:
                print("\n=== Updating G1 Arms ===")
                
                # Get current joint positions from physics
                prefix = "g1_29dof_rev_1_0/"
                all_joints = self._left_arm_joints + self._right_arm_joints
                current_q = np.zeros(self._mink_config.nq)
                
                # Track all joint positions and velocities before update
                print("\nState before IK solve:")
                for joint_name in all_joints:
                    full_joint_name = prefix + joint_name
                    joint_id = physics.model.name2id(full_joint_name, "joint")
                    if joint_id >= 0:
                        current_q[joint_id] = physics.data.qpos[joint_id]
                        current_vel = physics.data.qvel[joint_id]
                        print(f"{joint_name}:")
                        print(f"  Position: {physics.data.qpos[joint_id]:.6f}")
                        print(f"  Velocity: {current_vel:.6f}")
                
                # Update mink configuration with current joint positions
                self._mink_config.update(q=current_q)
                
                # Print initial hand positions for verification
                print("\n=== Hand Position Debug ===")
                print(f"Shadow hand positions:")
                print(f"  Left hand: {hand_positions['left']}")
                print(f"  Right hand: {hand_positions['right']}")
                
                # Update hand task targets with higher gains for better tracking
                hand_mapping = {
                    'right': hand_positions['right'],
                    'left': hand_positions['left']
                }
                
                print("\n=== Task Assignment Debug ===")
                for i, (task, side) in enumerate(zip(self._mink_tasks, ['right', 'left'])):
                    print(f"\nTask {i} Debug:")
                    print(f"Assigned to: {side} arm")
                    frame_name = f"g1_29dof_rev_1_0/{side}_wrist_yaw_link"
                    frame_id = physics.model.name2id(frame_name, "body")
                    current_pos = physics.data.xpos[frame_id]
                    target_pos = hand_mapping[side]
                    
                    print(f"Frame name: {frame_name}")
                    print(f"Current position: {current_pos}")
                    print(f"Target position: {target_pos}")
                    print(f"Distance to target: {np.linalg.norm(target_pos - current_pos):.6f}")
                    
                    # Print relative position to verify crossing
                    if side == 'right':
                        print(f"Right arm Y position: {current_pos[1]:.6f} (should be positive)")
                    else:
                        print(f"Left arm Y position: {current_pos[1]:.6f} (should be negative)")
                    
                    wxyz_xyz = np.zeros(7)
                    wxyz_xyz[0] = 1.0  # w component of quaternion
                    wxyz_xyz[4:] = hand_mapping[side]  # xyz position
                    
                    target = mink.SE3(wxyz_xyz=wxyz_xyz)
                    task.set_target(target)
                
                rate = RateLimiter(frequency=10.0, warn=False)

                # Solve IK with improved parameters
                vel = mink.solve_ik(
                    self._mink_config,
                    self._mink_tasks,
                    rate.dt,
                    "osqp",
                    damping=10.0,  # Increased damping
                    limits=None
                )
                
                # Scale velocities more conservatively
                vel_scale = 2.0  # Reduced from 10.0
                vel = vel * vel_scale
                
                # Apply stricter velocity limits
                vel_limit = 2.0  # Reduced from 10.0
                vel = np.clip(vel, -vel_limit, vel_limit)
                
                # Add acceleration limiting with lower limits
                if hasattr(self, '_prev_vel'):
                    acc = (vel - self._prev_vel) / rate.dt
                    acc_limit = 20.0  # Reduced from 100.0
                    acc_limited = np.clip(acc, -acc_limit, acc_limit)
                    vel = self._prev_vel + acc_limited * rate.dt
                self._prev_vel = vel.copy()
                
                print(f"\nIK Solution:")
                print(f"Velocity norm: {np.linalg.norm(vel):.6f}")
                
                # Print velocity commands for each joint
                print("\nVelocity commands:")
                for i, joint_name in enumerate(all_joints):
                    joint_id = physics.model.name2id(prefix + joint_name, "joint")
                    if joint_id >= 0:
                        print(f"{joint_name}: {vel[joint_id]:.6f}")

                # Apply both position and velocity control
                self._mink_config.integrate_inplace(vel, rate.dt)
                
                # Track final positions and movement
                print("\nState after IK solve:")
                for joint_name in all_joints:
                    joint_element = self._g1.mjcf_model.find('joint', joint_name)
                    if joint_element is not None:
                        old_pos = physics.bind(joint_element).qpos[0]  # Get first (and only) element
                        new_pos = float(self._mink_config.q[physics.model.name2id(prefix + joint_name, "joint")])
                        
                        # Apply position and velocity directly to physics
                        physics.bind(joint_element).qpos = new_pos
                        physics.bind(joint_element).qvel = float(vel[physics.model.name2id(prefix + joint_name, "joint")])
                        
                        # Apply additional velocity-based position correction with higher gain
                        pos_error = new_pos - old_pos
                        correction_vel = pos_error / rate.dt
                        physics.bind(joint_element).qvel += correction_vel * 0.8  # Increased from 0.5
                        
                        delta = new_pos - old_pos
                        print(f"{joint_name}:")
                        print(f"  Old pos: {old_pos:.6f}")
                        print(f"  New pos: {new_pos:.6f}")
                        print(f"  Delta: {delta:.6f}")
                        print(f"  Current vel: {physics.bind(joint_element).qvel[0]:.6f}")  # Get first element for velocity
                    else:
                        print(f"Warning: Could not find joint element for {joint_name}")

                # Store velocities for use between IK updates
                self._last_velocities = {}
                for joint_name in all_joints:
                    joint_element = self._g1.mjcf_model.find('joint', joint_name)
                    if joint_element is not None:
                        self._last_velocities[joint_name] = physics.bind(joint_element).qvel[0]

                print("\n=== End Updating G1 Arms ===\n")

            except Exception as e:
                print(f"Error in _update_g1_arms inner try block: {str(e)}")
                import traceback
                traceback.print_exc()
            
        except Exception as e:
            print(f"Error in _update_g1_arms outer try block: {str(e)}")
            import traceback
            traceback.print_exc()

    def before_step(
        self,
        physics: mjcf.Physics,
        action: np.ndarray,
        random_state: np.random.RandomState,
    ) -> None:
        """Updates the environment using the control signal."""
        super().before_step(physics, action, random_state)
        
        # Get shadow hand positions and update G1 arms
        hand_positions = self._get_shadow_hand_positions(physics)
        
        # Only run IK every _ik_update_rate steps
        if self._ik_counter % self._ik_update_rate == 0:
            self._update_g1_arms(physics, hand_positions)
            self._ik_counter = 0
        else:
            # If not running IK, use the last computed velocities if available
            if self._last_velocities is not None:
                prefix = "g1_29dof_rev_1_0/"
                all_joints = self._left_arm_joints + self._right_arm_joints
                for joint_name in all_joints:
                    joint_element = self._g1.mjcf_model.find('joint', joint_name)
                    if joint_element is not None:
                        physics.bind(joint_element).qvel = self._last_velocities[joint_name]
        
        self._ik_counter += 1

    def after_step(
        self,
        physics: mjcf.Physics,
        random_state: np.random.RandomState
    ) -> None:
        """Update camera position and handle other post-step operations."""
        # First call parent's after_step
        super().after_step(physics, random_state)

        # Update camera position - only rotate in the horizontal plane
        self._camera_angle += self._camera_angular_velocity
        new_x = self._camera_radius * np.cos(self._camera_angle)
        new_y = self._camera_radius * np.sin(self._camera_angle)
        
        # Update camera position in physics
        camera = physics.bind(self._camera)
        camera.pos = [new_x, new_y, self._camera_height]
        
        # Calculate look direction vector (pointing horizontally)
        look_dir = np.array([-new_x, -new_y, 0])  # Point towards center but keep horizontal
        look_dir = look_dir / np.linalg.norm(look_dir)
        
        # Fixed up vector (world up)
        up = np.array([0, 0, 1])
        
        # Calculate right vector
        right = np.cross(look_dir, up)
        right = right / np.linalg.norm(right)
        
        # Recalculate up to ensure orthogonality
        up = np.cross(right, look_dir)
        up = up / np.linalg.norm(up)
        
        # Create rotation matrix [right, up, -look_dir]
        rot_matrix = np.array([right, up, -look_dir]).T
        
        # Convert rotation matrix to quaternion
        trace = np.trace(rot_matrix)
        if trace > 0:
            S = np.sqrt(trace + 1.0) * 2
            qw = 0.25 * S
            qx = (rot_matrix[2, 1] - rot_matrix[1, 2]) / S
            qy = (rot_matrix[0, 2] - rot_matrix[2, 0]) / S
            qz = (rot_matrix[1, 0] - rot_matrix[0, 1]) / S
        else:
            if rot_matrix[0, 0] > rot_matrix[1, 1] and rot_matrix[0, 0] > rot_matrix[2, 2]:
                S = np.sqrt(1.0 + rot_matrix[0, 0] - rot_matrix[1, 1] - rot_matrix[2, 2]) * 2
                qw = (rot_matrix[2, 1] - rot_matrix[1, 2]) / S
                qx = 0.25 * S
                qy = (rot_matrix[0, 1] + rot_matrix[1, 0]) / S
                qz = (rot_matrix[0, 2] + rot_matrix[2, 0]) / S
            elif rot_matrix[1, 1] > rot_matrix[2, 2]:
                S = np.sqrt(1.0 + rot_matrix[1, 1] - rot_matrix[0, 0] - rot_matrix[2, 2]) * 2
                qw = (rot_matrix[0, 2] - rot_matrix[2, 0]) / S
                qx = (rot_matrix[0, 1] + rot_matrix[1, 0]) / S
                qy = 0.25 * S
                qz = (rot_matrix[1, 2] + rot_matrix[2, 1]) / S
            else:
                S = np.sqrt(1.0 + rot_matrix[2, 2] - rot_matrix[0, 0] - rot_matrix[1, 1]) * 2
                qw = (rot_matrix[1, 0] - rot_matrix[0, 1]) / S
                qx = (rot_matrix[0, 2] + rot_matrix[2, 0]) / S
                qy = (rot_matrix[1, 2] + rot_matrix[2, 1]) / S
                qz = 0.25 * S
        
        # Update camera orientation
        camera.quat = [qw, qx, qy, qz]

    def get_reward(self, physics: mjcf.Physics) -> float:
        return self._reward_fn.compute(physics)

    def get_discount(self, physics: mjcf.Physics) -> float:
        del physics  # Unused.
        return self._discount

    def should_terminate_episode(self, physics: mjcf.Physics) -> bool:
        del physics  # Unused.
        if self._should_terminate:
            return True
        if self._wrong_press_termination and self._failure_termination:
            self._discount = 0.0
            return True
        return False

    @property
    def task_observables(self):
        """Returns the task observables from parent class."""
        return super().task_observables  # Use parent's observables instead of our own

    def action_spec(self, physics: mjcf.Physics) -> specs.BoundedArray:
        right_spec = self.right_hand.action_spec(physics)
        left_spec = self.left_hand.action_spec(physics)
        hands_spec = spec_utils.merge_specs([right_spec, left_spec])
        sustain_spec = specs.BoundedArray(
            shape=(1,),
            dtype=hands_spec.dtype,
            minimum=[0.0],
            maximum=[1.0],
            name="sustain",
        )
        return spec_utils.merge_specs([hands_spec, sustain_spec])

    # Other.

    @property
    def midi(self) -> midi_file.MidiFile:
        return self._midi

    @property
    def reward_fn(self) -> composite_reward.CompositeReward:
        return self._reward_fn

    # Helper methods.

    def _compute_forearm_reward(self, physics: mjcf.Physics) -> float:
        """Reward for not colliding the forearms."""
        if collision_utils.has_collision(
            physics,
            [g.full_identifier for g in self.right_hand.root_body.geom],
            [g.full_identifier for g in self.left_hand.root_body.geom],
        ):
            return 0.0
        return 0.5

    def _compute_sustain_reward(self, physics: mjcf.Physics) -> float:
        """Reward for pressing the sustain pedal at the right time."""
        del physics  # Unused.
        return tolerance(
            self._goal_current[-1] - self.piano.sustain_activation[0],
            bounds=(0, _KEY_CLOSE_ENOUGH_TO_PRESSED),
            margin=(_KEY_CLOSE_ENOUGH_TO_PRESSED * 10),
            sigmoid="gaussian",
        )

    def _compute_energy_reward(self, physics: mjcf.Physics) -> float:
        """Reward for minimizing energy."""
        rew = 0.0
        for hand in [self.right_hand, self.left_hand]:
            power = hand.observables.actuators_power(physics).copy()
            rew -= self._energy_penalty_coef * np.sum(power)
        return rew

    def _compute_key_press_reward(self, physics: mjcf.Physics) -> float:
        """Reward for pressing the right keys at the right time."""
        del physics  # Unused.
        on = np.flatnonzero(self._goal_current[:-1])
        rew = 0.0
        # It's possible we have no keys to press at this timestep, so we need to check
        # that `on` is not empty.
        if on.size > 0:
            actual = np.array(self.piano.state / self.piano._qpos_range[:, 1])
            rews = tolerance(
                self._goal_current[:-1][on] - actual[on],
                bounds=(0, _KEY_CLOSE_ENOUGH_TO_PRESSED),
                margin=(_KEY_CLOSE_ENOUGH_TO_PRESSED * 10),
                sigmoid="gaussian",
            )
            rew += 0.5 * rews.mean()
        # If there are any false positives, the remaining 0.5 reward is lost.
        off = np.flatnonzero(1 - self._goal_current[:-1])
        rew += 0.5 * (1 - float(self.piano.activation[off].any()))
        return rew

    def _compute_fingering_reward(self, physics: mjcf.Physics) -> float:
        """Reward for minimizing the distance between the fingers and the keys."""

        def _distance_finger_to_key(
            hand_keys: List[Tuple[int, int]], hand
        ) -> List[float]:
            distances = []
            for key, mjcf_fingering in hand_keys:
                fingertip_site = hand.fingertip_sites[mjcf_fingering]
                fingertip_pos = physics.bind(fingertip_site).xpos.copy()
                key_geom = self.piano.keys[key].geom[0]
                key_geom_pos = physics.bind(key_geom).xpos.copy()
                key_geom_pos[-1] += 0.5 * physics.bind(key_geom).size[2]
                key_geom_pos[0] += 0.35 * physics.bind(key_geom).size[0]
                diff = key_geom_pos - fingertip_pos
                distances.append(float(np.linalg.norm(diff)))
            return distances

        distances = _distance_finger_to_key(self._rh_keys_current, self.right_hand)
        distances += _distance_finger_to_key(self._lh_keys_current, self.left_hand)

        # Case where there are no keys to press at this timestep.
        if not distances:
            return 0.0

        rews = tolerance(
            np.hstack(distances),
            bounds=(0, _FINGER_CLOSE_ENOUGH_TO_KEY),
            margin=(_FINGER_CLOSE_ENOUGH_TO_KEY * 10),
            sigmoid="gaussian",
        )
        return float(np.mean(rews))

    def _compute_ot_fingering_reward(self, physics: mjcf.Physics) -> float:
        """ OT reward calculation from RP1M https://arxiv.org/abs/2408.11048 """
        # calcuate fingertip positions
        fingertip_pos = [physics.bind(finger).xpos.copy() for finger in self.left_hand.fingertip_sites]
        fingertip_pos += [physics.bind(finger).xpos.copy() for finger in self.right_hand.fingertip_sites]
        
        # calcuate the positions of piano keys to press.
        keys_to_press = np.flatnonzero(self._goal_current[:-1]) # keys to press
        # if no key is pressed
        if keys_to_press.shape[0] == 0:
            return 1.

        # calculate key pos
        key_pos = []
        for key in keys_to_press:
            key_geom = self.piano.keys[key].geom[0]
            key_geom_pos = physics.bind(key_geom).xpos.copy()
            key_geom_pos[-1] += 0.5 * physics.bind(key_geom).size[2]
            key_geom_pos[0] += 0.35 * physics.bind(key_geom).size[0]
            key_pos.append(key_geom_pos.copy())

        # calcualte the distance between keys and fingers
        dist = np.full((len(fingertip_pos), len(key_pos)), 100.)
        for i, finger in enumerate(fingertip_pos):
            for j, key in enumerate(key_pos):
                dist[i, j] = np.linalg.norm(key - finger)
        
        # calculate the shortest distance
        row_ind, col_ind = linear_sum_assignment(dist)
        dist = dist[row_ind, col_ind]
        rews = tolerance(
            dist,
            bounds=(0, _FINGER_CLOSE_ENOUGH_TO_KEY),
            margin=(_FINGER_CLOSE_ENOUGH_TO_KEY * 10),
            sigmoid="gaussian",
        )
        return float(np.mean(rews))        

    def _update_goal_state(self) -> None:
        # Observable callables get called after `after_step` but before
        # `should_terminate_episode`. Since we increment `self._t_idx` in `after_step`,
        # we need to guard against out of bounds indexing. Note that the goal state
        # does not matter at this point since we are terminating the episode and this
        # update is usually meant for the next timestep.
        if self._t_idx == len(self._notes):
            return

        self._goal_state = np.zeros(
            (self._n_steps_lookahead + 1, self.piano.n_keys + 1),
            dtype=np.float64,
        )
        t_start = self._t_idx
        t_end = min(t_start + self._n_steps_lookahead + 1, len(self._notes))
        for i, t in enumerate(range(t_start, t_end)):
            keys = [note.key for note in self._notes[t]]
            self._goal_state[i, keys] = 1.0
            self._goal_state[i, -1] = self._sustains[t]

    def _update_fingering_state(self) -> None:
        if self._t_idx == len(self._notes):
            return

        fingering = [note.fingering for note in self._notes[self._t_idx]]
        fingering_keys = [note.key for note in self._notes[self._t_idx]]

        # Split fingering into right and left hand.
        self._rh_keys: List[Tuple[int, int]] = []
        self._lh_keys: List[Tuple[int, int]] = []
        for key, finger in enumerate(fingering):
            piano_key = fingering_keys[key]
            if finger < 5:
                self._rh_keys.append((piano_key, finger))
            else:
                self._lh_keys.append((piano_key, finger - 5))

        # For each hand, set the finger to 1 if it is used and 0 otherwise.
        self._fingering_state = np.zeros((2, 5), dtype=np.float64)
        for hand, keys in enumerate([self._rh_keys, self._lh_keys]):
            for key, mjcf_fingering in keys:
                self._fingering_state[hand, mjcf_fingering] = 1.0

    def _add_observables(self) -> None:
        # Enable hand observables.
        enabled_observables = [
            "joints_pos",
            # NOTE(kevin): This observable was previously enabled but it is redundant
            # since it is encoded in the joint positions, specifically via the forearm
            # slider joints (which are in units of meters).
            # "position",
        ]
        for hand in [self.right_hand, self.left_hand]:
            for obs in enabled_observables:
                getattr(hand.observables, obs).enabled = True

        # This returns the current state of the piano keys.
        self.piano.observables.state.enabled = True
        self.piano.observables.sustain_state.enabled = True

        # This returns the goal state for the current timestep and n steps ahead.
        def _get_goal_state(physics) -> np.ndarray:
            del physics  # Unused.
            self._update_goal_state()
            return self._goal_state.ravel()

        goal_observable = observable.Generic(_get_goal_state)
        goal_observable.enabled = True
        self._task_observables = {"goal": goal_observable}

        # This adds fingering information for the current timestep.
        def _get_fingering_state(physics) -> np.ndarray:
            del physics  # Unused.
            self._update_fingering_state()
            return self._fingering_state.ravel()

        fingering_observable = observable.Generic(_get_fingering_state)
        fingering_observable.enabled = not self._disable_fingering_reward
        self._task_observables["fingering"] = fingering_observable

    def _colorize_fingertips(self) -> None:
        """Colorize the fingertips of the hands."""
        for hand in [self.right_hand, self.left_hand]:
            for i, body in enumerate(hand.fingertip_bodies):
                color = hand_consts.FINGERTIP_COLORS[i] + (_FINGERTIP_ALPHA,)
                for geom in body.find_all("geom"):
                    if geom.dclass.dclass == "plastic_visual":
                        geom.rgba = color
                # Also color the fingertip sites.
                hand.fingertip_sites[i].rgba = color

    def _colorize_keys(self, physics) -> None:
        """Colorize the keys by the corresponding fingertip color."""
        for hand, keys in zip(
            [self.right_hand, self.left_hand],
            [self._rh_keys_current, self._lh_keys_current],
        ):
            for key, mjcf_fingering in keys:
                key_geom = self.piano.keys[key].geom[0]
                fingertip_site = hand.fingertip_sites[mjcf_fingering]
                if not self.piano.activation[key]:
                    physics.bind(key_geom).rgba = tuple(fingertip_site.rgba[:3]) + (
                        1.0,
                    )

    def _disable_collisions_between_hands(self) -> None:
        """Disable collisions between the hands."""
        for hand in [self.right_hand, self.left_hand]:
            for geom in hand.mjcf_model.find_all("geom"):
                # If both hands have the same contype and conaffinity, then they can't
                # collide. They can still collide with the piano since the piano has
                # contype 0 and conaffinity 1. Lastly, we make sure we're not changing
                # the contype and conaffinity of the hand geoms that are already
                # disabled (i.e., the visual geoms).
                commit_defaults(geom, ["contype", "conaffinity"])
                if geom.contype == 0 and geom.conaffinity == 0:
                    continue
                geom.conaffinity = 0
                geom.contype = 1

    def _disable_collisions_between_hands_and_g1(self) -> None:
        """Disable collisions between the shadow hands and G1."""
        if not hasattr(self, '_g1') or self._g1 is None:
            return
        
        # Set G1 to not generate any contacts with hands
        for geom in self._g1.mjcf_model.find_all('geom'):
            geom.contype = 0  # Will not generate contacts
            #dont recieve any contacts
            geom.conaffinity = 0
        

    def _randomize_initial_hand_positions(
        self, physics: mjcf.Physics, random_state: np.random.RandomState
    ) -> None:
        """Randomize the initial position of the hands."""
        if not self._randomize_hand_positions:
            return
        offset = random_state.uniform(low=-_POSITION_OFFSET, high=_POSITION_OFFSET)
        for hand in [self.right_hand, self.left_hand]:
            hand.shift_pose(physics, (0, offset, 0))

    def _raise_piano(self):
        """Raise the piano position."""
        # Access the piano's root body and raise its position
        
        # Raise the piano base
        piano_base = self.piano.mjcf_model.find('body', 'base')
        if piano_base is not None:
            print("Piano base body found")
            current_pos = piano_base.pos
            if current_pos is not None:
                print(f"Current piano position: {current_pos}")
                piano_base.pos = (current_pos[0], current_pos[1], current_pos[2] + self._height_offset)
                print(f"New piano position: {piano_base.pos}")
        
        # Raise all piano keys
        for i in range(88):  # Piano has 88 keys
            for key_type in ['white_key_', 'black_key_']:
                key = self.piano.mjcf_model.find('body', f'{key_type}{i}')
                if key is not None:
                    current_pos = key.pos
                    if current_pos is not None:
                        key.pos = (current_pos[0], current_pos[1], current_pos[2] + self._height_offset)
