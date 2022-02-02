#!/usr/bin/env python3
#
#   HW#4 P5 straightline.py
#
#   Visualize the 3DOF, moving up in a task-space move, down in a
#   joint-space move.
#
#   Publish:   /joint_states   sensor_msgs/JointState
#
import rospy
import numpy as np
import matplotlib.pyplot as plt

from sensor_msgs.msg     import JointState
from std_msgs.msg        import Bool
from urdf_parser_py.urdf import Robot

# Import the kinematics stuff:
from force_kin import *
# We could also import the whole thing ("import kinematics"),
# but then we'd have to write "kinematics.p_from_T()" ...

# Import the Spline stuff:
from splines import *

#
#  Generator Class
#
class Generator:
    # Initialize.
    def __init__(self, sim=False):
        # Create a publisher to send the joint commands.  Add some time
        # for the subscriber to connect.  This isn't necessary, but means
        # we don't start sending messages until someone is listening.
        self.pub = rospy.Publisher("/hebi/joint_commands", JointState, queue_size=10)
        self.rviz_pub = rospy.Publisher("/joint_states", JointState, queue_size=10)
        rospy.sleep(0.25)

        # Grab the robot's URDF from the parameter server.
        robot = Robot.from_parameter_server()

        # Instantiate the Kinematics
        inertial_params = np.array([[0, 0],
                                  [-.2, .8],
                                  [0, -0.1],])
        self.kin = Kinematics(robot, 'world', 'tip', inertial_params=inertial_params)

        # Set the tip targets (in 3x1 column vectors).
        xA = np.array([ 0.07, 0.0, 0.15]).reshape((3,1))    # Bottom.
        xB = np.array([-0.07, 0.0, 0.15]).reshape((3,1))    # Top.

        # Create the splines.
        self.sin_traj = SinTraj(xA, xB, np.inf, .5, space="Cart")
        self.segments = [self.sin_traj]
        self.segment_q = list()

        # Initialize the current segment index and starting time t0.
        self.index = 0
        self.t0    = 0.0
        self.old_t0 = 0.0
        
        self.last_t = None
        self.sim = sim

        # Also initialize the storage of the last joint position
        # (angles) to where the first segment starts.

        if self.sim == False:
            msg = rospy.wait_for_message('/hebi/joint_states', JointState)
            self.lasttheta_state = self.lasttheta = np.array(msg.position).reshape((3,1))
            self.lastthetadot_state = self.lastthetadot = np.array(msg.velocity).reshape((3,1))
        else:
            self.lasttheta_state = self.lasttheta = np.pi * np.random.rand(3, 1)
            self.lastthetadot_state = self.lastthetadot = self.lasttheta * 0
            #self.lasttheta = np.array([1.49567867, 0.95467971, 2.29442065]).reshape((3,1))    # Test case
            

        # Pick the initial estimate (in a 3x1 column vector).
        theta0 = np.array([0.0, np.pi/2, -np.pi/2]).reshape((3,1))

        # Flips between 1 and -1 every time the robot does a flip.
        # Not critical for functionality, but ensures that the robot doesn't
        # keep spinning the same way on the vertical axis.
        self.orientation = 1
        
        self.is_resetting = False

        # Add spline which goes to the correct starting position
        self.reset(duration = 4)

        # Subscribe to "/switch" which causes the robot to do a flip
        self.switch_sub = rospy.Subscriber("/switch", Bool, self.switch_callback)

        # Subscriber which listens to the motors' positions and velocities
        self.state_sub = rospy.Subscriber('/hebi/joint_states', JointState, self.state_update_callback)
        
        self.theta_history = []
        self.thetadot_history = []

    def reset(self, duration = 10):
        rospy.loginfo("Resetting robot")
        # Compute desired theta, starting the Newton Raphson at the last theta.
        goal_pos, _ = self.segments[0].evaluate(0)
        goal_theta = np.fmod(self.ikin(goal_pos, self.lasttheta), 2*np.pi)

        # Choose fastest path to the start
        for i in range(goal_theta.shape[0]):
            candidate = np.remainder(goal_theta[i,0], 2*np.pi)
            if abs(candidate - self.lasttheta[i,0]) < abs(goal_theta[i,0] - self.lasttheta[i,0]):
                goal_theta[i,0] = candidate

        # Don't go through the table
        if -np.pi < goal_theta[1] < 0:
            goal_theta[1] = 0*np.pi - goal_theta[1]
            goal_theta[2] = 0*np.pi - goal_theta[2]
        if 2*np.pi > goal_theta[1] > np.pi:
            goal_theta[1] = 2*np.pi - goal_theta[1]
            goal_theta[2] = 0*np.pi - goal_theta[2]

        # Don't go thru itself
        if goal_theta[2,0] < -np.pi < self.lasttheta[2,0]:
            goal_theta[2,0] += 2*np.pi
        if goal_theta[2,0] > -np.pi > self.lasttheta[2,0]:
            goal_theta[2,0] -= 2*np.pi
        if goal_theta[2,0] < np.pi < self.lasttheta[2,0]:
            goal_theta[2,0] += 2*np.pi
        if goal_theta[2,0] > np.pi > self.lasttheta[2,0]:
            goal_theta[2,0] -= 2*np.pi

        #self.segment_q.append(QuinticSpline(self.lasttheta, self.lastthetadot, 0, goal_theta, 0, 0, duration))
        self.segment_q.append(CubicSpline(self.lasttheta, -self.lastthetadot, goal_theta, 0, duration, rm=True))
        self.is_resetting = True

    def flip(self, duration = 4):
        # Convert all angles to be between 0 and 2pi
        rounds = np.floor_divide(self.lasttheta, np.pi*2)

        # Hard-code solution to flipped arm
        thetaInit = np.remainder(self.lasttheta, np.pi*2)
        thetaGoal = (thetaInit.T * np.array([1, -1, -1])
                                 + np.array([self.orientation * np.pi, np.pi, 0])).reshape((3, 1))

        # Ensures that the robot arm segments don't collide with each other.
        if thetaInit[2, 0] > np.pi:
            thetaGoal[2, 0] += 4*np.pi

        thetaDotInit = self.lastthetadot
        thetaDotGoal = (thetaDotInit.T * np.array([1, -1, -1])).reshape((3, 1))

        # Flips between -1 and 1
        self.orientation *= -1
        
        (T,J)     = self.kin.fkin(thetaInit)
        initPos = p_from_T(T)

        # Convert angles back to original space
        thetaGoal += rounds * 2*np.pi
        thetaGoal = self.ikin(initPos, thetaGoal)
        thetaInit += rounds * 2*np.pi
        self.segment_q.append(QuinticSpline(thetaInit, thetaDotInit, 0, thetaGoal, thetaDotGoal, 0, duration, rm=True))

    def switch_callback(self, msg):
        # msg is unused
        if self.is_oscillating():
            self.flip(duration=8)

    def state_update_callback(self, msg):
        # Update our knowledge of true position and velocity of the motors
        #rospy.loginfo("Recieved state update message " + str(msg))
        self.lasttheta_state = np.array(msg.position).reshape((3,1))
        self.lastthetadot_state = np.array(msg.velocity).reshape((3, 1))

    def is_contacting(self):
        theta_error = np.sum(np.abs(self.lasttheta_state.reshape(-1) - self.lasttheta.reshape(-1)))
        thetadot_error = np.sum(np.abs(self.lastthetadot_state.reshape(-1) - self.lastthetadot.reshape(-1)))
        #print(theta_error, thetadot_error)
        return (theta_error > 0.075)
        
    def is_oscillating(self):
        return isinstance(self.segments[self.index], SinTraj)
    

    # Newton-Raphson static (indpendent of time/motion) Inverse Kinematics:
    # Iterate to find the joints values putting the tip at the goal.
    def ikin(self, pgoal, theta_initialguess):
        # Start iterating from the initial guess
        theta = theta_initialguess

        # Iterate at most 50 times (just to be safe)!
        for i in range(50):
            # Figure out where the current joint values put us:
            # Compute the forward kinematics for this iteration.
            (T,J) = self.kin.fkin(theta)

            # Extract the position and use only the 3x3 linear
            # Jacobian (for 3 joints).  Generalize this for bigger
            # mechanisms if/when we need.
            p = p_from_T(T)
            J = J[0:3, 0:3]

            # Compute the error.  Generalize to also compute
            # orientation errors if/when we need.
            e = pgoal - p

            # Take a step in the appropriate direction.  Using an
            # "inv()" though ultimately a "pinv()" would be safer.
            theta = theta + np.linalg.inv(J) @ e

            # Return if we have converged.
            if (np.linalg.norm(e) < 1e-6):
                return theta

        # After 50 iterations, return the failure and zero instead!
        rospy.logwarn("Unable to converge to [%f,%f,%f]",
                      pgoal[0], pgoal[1], pgoal[2]);
        return 0 * theta


    # Update is called every 10ms!
    def update(self, t):
        dt = t - self.last_t
        self.last_t = t
        if self.segment_q:
            self.index = len(self.segments)
            self.segments += self.segment_q
            self.segment_q = list()
            self.old_t0 = self.t0
            self.t0 = t

        # If the current segment is done, shift to the next.
        dur = self.segments[self.index].duration()
        if (t - self.t0 >= dur):
            self.t0    = (self.t0    + dur)
            #self.index = (self.index + 1)                       # not cyclic!
            self.index = (self.index + 1) % len(self.segments)  # cyclic!
            if self.index < len(self.segments) and self.is_oscillating():
                self.t0 = self.old_t0
            if self.is_resetting:
                self.is_resetting = False
                self.t0 = t
            rm = []
            for i in range(len(self.segments)):
                if self.segments[i].rm == True:
                    rm.append(i)
            for i in rm[::-1]:
                self.segments.pop(i)

        # Check whether we are done with all segments.
        if (self.index >= len(self.segments)):
            rospy.signal_shutdown("Done with motion")
            return

        # Decide what to do based on the space.
        if (self.segments[self.index].space() == 'Joint'):
            # Grab the spline output as joint values.
            (theta, thetadot) = self.segments[self.index].evaluate(t - self.t0)
        else:
            # Grab the spline output as task values.
            (p, v)    = self.segments[self.index].evaluate(t - self.t0)
            #Robj = R_from_rpy(x[3:6])
            
            # Get the Jacobian and T matrix
            T, J = self.kin.fkin(self.lasttheta[:, 0])
            
            # Get the weighted Jacobian pseudoinverse
            #gamma = .05
            #Jw_inv = J.T @ np.linalg.pinv(J @ J.T + gamma**2 * np.eye(len(J[:, 0])))
            Jw_inv = np.linalg.pinv(J)
                
            # Get the real tip position x(q)
            xtip = p_from_T(T)
            Rtip = R_from_T(T)
            
            # Error term
            e_p = ep(p[0:3], xtip)
            #e_r = eR(Robj, Rtip)
            #e = np.append(e_p, e_r)

            # Setting secondary task to centering
            c_repulse = 5
            
            # Getting angle velocities
            lam = 1/dt
            thetadot = (Jw_inv[0:3, 0:3] @ (v[0:3] + lam*e_p)).reshape(3, 1) #+ (np.eye(N) - Jw_inv @ J) @ theta_second
            

        # Save the position (to be used as an estimate next time).
        self.lasttheta = self.lasttheta + dt * thetadot
        self.lastthetadot = thetadot

        # Create and send the command message.  Note the names have to
        # match the joint names in the URDF.  And their number must be
        # the number of position/velocity elements.
        cmdmsg = JointState()
        cmdmsg.name         = ['Thor/1', 'Thor/2', 'Thor/3']
        cmdmsg.position     = self.lasttheta
        cmdmsg.velocity     = self.lastthetadot
        cmdmsg.effort       = self.kin.grav(self.lasttheta_state)
        cmdmsg.header.stamp = rospy.Time.now()
        self.pub.publish(cmdmsg)
        self.rviz_pub.publish(cmdmsg)

        if not self.sim and self.is_contacting() and self.is_oscillating():
            self.reset(duration=4)
#
#
#  Main Code
#
if __name__ == "__main__":
    
    # Prepare/initialize this node.
    rospy.init_node('straightline')

    # Instantiate the trajectory generator object, encapsulating all
    # the computation and local variables.
    generator = Generator(sim=True)

    # Prepare a servo loop at 100Hz.
    rate  = 100;
    servo = rospy.Rate(rate)
    dt    = servo.sleep_dur.to_sec()
    rospy.loginfo("Running the servo loop with dt of %f seconds (%fHz)" %
                  (dt, rate))


    # Run the servo loop until shutdown (killed or ctrl-C'ed).
    starttime = rospy.Time.now()
    generator.last_t = starttime.to_sec()
    while not rospy.is_shutdown():

        # Current time (since start)
        servotime = rospy.Time.now()
        t = (servotime - starttime).to_sec()

        # Update the controller.
        generator.update(t)

        # Wait for the next turn.  The timing is determined by the
        # above definition of servo.
        servo.sleep()
