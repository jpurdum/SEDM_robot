import logging
from logging.handlers import TimedRotatingFileHandler
import time
import socket
import os
import sys
import json
import SEDM_robot_version as Version

with open(os.path.join(Version.CONFIG_DIR, 'logging.json')) as data_file:
    params = json.load(data_file)

logger = logging.getLogger("stageControllerLogger")
logger.setLevel(logging.DEBUG)
logging.Formatter.converter = time.gmtime
logHandler = TimedRotatingFileHandler(os.path.join(params['abspath'],
                                                   'stage_controller.log'),
                                      when='midnight', utc=True, interval=1,
                                      backupCount=360)

formatter = logging.Formatter("%(asctime)s--%(name)s--%(levelname)s--"
                              "%(module)s--%(funcName)s--%(message)s")
logHandler.setFormatter(formatter)
logHandler.setLevel(logging.DEBUG)
logger.addHandler(logHandler)

console_formatter = logging.Formatter("%(asctime)s--%(message)s")
consoleHandler = logging.StreamHandler(sys.stdout)
consoleHandler.setFormatter(console_formatter)
logger.addHandler(consoleHandler)

logger.info("Starting Logger: Logger file is %s", 'stage_controller.log')


class Stage:
    """The following stage controller commands are available. Note that many
    are not implemented at the moment.  The asterisk indicates the commands
    that are implemented.

    AC    Set/Get acceleration
    BA    Set/Get backlash compensation
    BH    Set/Get hysteresis compensation
    DV    Set/Get driver voltage Not for PP
    FD    Set/Get low pass filter for Kd Not for PP
    FE    Set/Get following error limit Not for PP
    FF    Set/Get friction compensation Not for PP
    FR    Set/Get stepper motor configuration Not for CC
    HT  * Set/Get HOME search type
    ID    Set/Get stage identifier
    JD    Leave JOGGING state
    JM    Enable/disable keypad
    JR    Set/Get jerk time
    KD    Set/Get derivative gain Not for PP
    KI    Set/Get integral gain Not for PP
    KP    Set/Get proportional gain Not for PP
    KV    Set/Get velocity feed forward Not for PP
    MM    Enter/Leave DISABLE state
    OH    Set/Get HOME search velocity
    OR  * Execute HOME search
    OT    Set/Get HOME search time-out
    PA  * Move absolute
    PR    Move relative
    PT    Get motion time for a relative move
    PW  * Enter/Leave CONFIGURATION state
    QI    Set/Get motor’s current limits
    RA    Get analog input value
    RB    Get TTL input value
    RS  * Reset controller
    SA    Set/Get controller’s RS-485 address
    SB    Set/Get TTL output value
    SC    Set/Get control loop state Not for PP
    SE    Configure/Execute simultaneous started move
    SL  * Set/Get negative software limit
    SR  * Set/Get positive software limit
    ST    Stop motion
    SU  * Set/Get encoder increment value Not for PP
    TB    Get command error string
    TE    Get last command error
    TH    Get set-point position
    TP  * Get current position
    TS  * Get positioner error and controller state
    VA    Set/Get velocity
    VB    Set/Get base velocity Not for CC
    VE    Get controller revision information
    ZT  * Get all axis parameters
    ZX  * Set/Get SmartStage configuration

    Values below as of 2023-Oct-17

    For stage 1, current values are:
    10 - Acceleration
    0 - negative software limit, from 1SL?
    3 - positive software limit, from 1SR?
    1.76994e-05 - units per encoder increment, from 1SU?

    For stage2, current values are:
    1.6 - Acceleration
    0 - negative software limit, from 2SL?
    8 - positive software limit, from 2SR?
    3.0518e-05 - units per encoder increment, from 2SU?

    """

    def __init__(self, host=None, port=None):
        """
        Class to handle communications with the stage controller and any faults

        :param host: host ip
        :param port: port socket number
        """

        with open(os.path.join(Version.CONFIG_DIR, 'stages.json')) as df:
            self.stage_config = json.load(df)

        if not host:
            self.host = self.stage_config['host']
        else:
            self.host = host
        if not port:
            self.port = self.stage_config['port']
        else:
            self.port = port

        logger.info("Initiating stage controller on host:"
                    " %(host)s port: %(port)s", {'host': self.host,
                                                 'port': self.port})

        self.socket = socket.socket()

        # nominal positions
        self.stage1_nom = self.stage_config['stage1']
        self.stage2_nom = self.stage_config['stage2']

        self.controller_commands = ["PA", "SU", "ZX1", "ZX2", "ZX3", "OR",
                                    "PW1", "PW0", "SL", "SR", "HT1",
                                    "TS", "TP", "ZT", "RS"]

        self.return_value_commands = ["TS", "TP"]
        self.parameter_commands = ["PA", "SU"]
        self.end_code_list = ['32', '33', '34', '35']
        self.not_ref_list = ['0A', '0B', '0C', '0D', '0F', '10', '11']
        self.moving_list = ['28']
        self.msg = {
            "0A": "NOT REFERENCED from reset.",
            "0B": "NOT REFERENCED from HOMING.",
            "0C": "NOT REFERENCED from CONFIGURATION.",
            "0D": "NOT REFERENCED from DISABLE.",
            "0E": "NOT REFERENCED from READY.",
            "0F": "NOT REFERENCED from MOVING.",
            "10": "NOT REFERENCED ESP stage error.",
            "11": "NOT REFERENCED from JOGGING.",
            "14": "CONFIGURATION.",
            "1E": "HOMING commanded from RS-232-C.",
            "1F": "HOMING commanded by SMC-RC.",
            "28": "MOVING.",
            "32": "READY from HOMING.",
            "33": "READY from MOVING.",
            "34": "READY from DISABLE.",
            "35": "READY from JOGGING.",
            "3C": "DISABLE from READY.",
            "3D": "DISABLE from MOVING.",
            "3E": "DISABLE from JOGGING.",
            "46": "JOGGING from READY.",
            "47": "JOGGING from DISABLE."
        }

    def __connect(self):
        try:
            logger.info("Connected to %(host)s:%(port)s", {'host': self.host,
                                                           'port': self.port})
            self.socket.connect((self.host, self.port))
        except OSError:
            logger.info("Already connected")
            pass
        except Exception as e:
            logger.info("Error connecting to the socket", exc_info=True)
            return str(e)

    def __send_serial_command(self, stage_id=1, cmd=''):
        """

        :param stage_id:
        :param cmd:
        :return:
        """

        # Prep command
        cmd_send = "%s%s\r\n" % (stage_id, cmd)
        logger.info("Sending command:%s", cmd_send)
        cmd_encoded = cmd_send.encode('utf-8')

        # Make connection
        self.__connect()
        self.socket.settimeout(30)

        # Send command
        self.socket.send(cmd_encoded)
        time.sleep(.05)
        recv = None

        # Return value commands
        if cmd.upper() in self.return_value_commands:

            # Get return value
            recv = self.socket.recv(2048)
            recv_len = len(recv)
            logger.info("Return: len = %d, Value = %s", recv_len, recv)

            # Are we a valid return value?
            if recv_len == 11 or recv_len == 12 or recv_len == 13:
                logger.info("Return value validated")
                return recv

        # Non-return value commands eventually return state output
        t = 300     # How many tries while waiting for command
        while t > 0:
            # Check state
            statecmd = '%sTS\r\n' % stage_id
            statecmd = statecmd.encode('utf-8')
            self.socket.send(statecmd)
            time.sleep(.05)
            recv = self.socket.recv(1024)

            # Valid state return
            if len(recv) == 11:
                # Parse state
                recv = recv.rstrip()
                code = str(recv[-2:].decode('utf-8'))

                # Valid end code (done)
                if code in self.end_code_list:
                    return recv

                # Not referenced code (done)
                elif code in self.not_ref_list:
                    return recv

                # else: moving, so loop until not

            # Invalid state return (done)
            else:
                logger.warning("Bad %dTS return: %s", stage_id, recv)
                return recv

            # Decrement try counter and read state again
            t -= 1

        # end while t > 0  (tries still left)

        # If we get here, we ran out of tries
        logger.warning("Command timed out, final state: %s", recv)
        return recv

    def __send_command(self, cmd="", parameters=None, stage_id=1,
                       custom_command=False, home_when_not_ref=True):
        """
        Send a command to the stage controller and keep checking the state
        until it matches one in the end_code

        :param cmd: string command to send to the camera socket
        :param parameters: list of parameters associated with cmd
        :return: Tuple (bool,string)
        """
        start = time.time()

        if not custom_command:
            if cmd.rstrip().upper() not in self.controller_commands:
                return {'elaptime': time.time()-start,
                        'error': "%s is not a valid command" % cmd}

        logger.info("Input command: %s", cmd)

        # Check if the command should have parameters
        if cmd in self.parameter_commands and parameters:
            logger.info("add parameters")
            parameters = [str(x) for x in parameters]
            parameters = " ".join(parameters)
            cmd += parameters
            logger.info(cmd)

        # Send command
        response = self.__send_serial_command(stage_id, cmd)
        response = response.decode('utf-8')
        logger.info("Response from stage controller %d: %s", stage_id, response)

        # Parse response
        message = self.__return_parse(response)

        # Next check if we expect a return value from command
        if cmd in self.return_value_commands:

            # Parse position return
            if cmd.upper() == 'TP':
                response = response.rstrip()
                return {'elaptime': time.time() - start, 'data': response[3:]}

            # Return whole message (usually from TS)
            else:
                return {'elaptime': time.time() - start, 'data': message}

        # Non-return value command, but stage in unknown state
        elif cmd not in self.return_value_commands and \
                message == "Unknown state":
            return {'elaptime': time.time() - start, 'error': response}

        # Not referenced (needs to be homed)
        elif 'REFERENCED' in message:
            if home_when_not_ref:
                # TODO: move to nominal positions after homing
                logger.info("State is NOT REFERENCED, Homing stage...")
                response = self.__send_serial_command(stage_id, 'OR')
                response = response.decode('utf-8')
                logger.info("Cmd response from stage controller: %s", response)
                message = self.__return_parse(response)
                return {'elaptime': time.time() - start, 'data': message}

            else:
                return {'elaptime': time.time() - start, 'error': message}

        # Valid state achieved after command
        else:
            return {'elaptime': time.time() - start, 'data': message}

        # except Exception as e:
        #     logger.error("Error in the stage controller return")
        #     logger.error(str(e))
        #     return -1 * (time.time() - start), str(e)

    def __return_parse(self, message=""):
        """
        Parse the return message from the controller.  The message code is
        given in the last two string characters

        :param message: message code from the controller
        :return: string message
        """
        message = message.rstrip()
        code = message[-2:]
        return self.msg.get(code, "Unknown state")

    def home(self, stage_id=1):
        """
        Home the stage
        :return: bool, status message
        """
        return self.__send_command(cmd='OR', stage_id=stage_id)

    def move_focus(self, position=12.5, stage_id=1):
        """
        Move stage and return when in position

        :return:bool, status message
        """
        return self.__send_command(cmd="PA", stage_id=stage_id,
                                   parameters=[position])

    def get_state(self, stage_id=1):
        return self.__send_command(cmd="TS", stage_id=stage_id)

    def get_position(self, stage_id=1):
        start = time.time()
        try:
            ret = self.__send_command(cmd="TP", stage_id=stage_id)
        except Exception as e:
            logger.error('get_position error: %s', str(e))
            ret = {'elaptime': time.time()-start,
                   'error': 'Unable to send stage command'}
        return ret

    # Not Used
    def enter_config_state(self, stage_id=1):
        """

        :param stage_id:
        :return:
        """
        # cmd = ""
        # end_code = None

        logger.warning("WARNING YOU ARE ABOUT TO ENTER THE CONFIGURATION "
                       "STATE.\nPLEASE DON'T MAKE ANY CHANGES UNLESS YOU "
                       "KNOW WHAT YOU ARE DOING")
        input("Press Enter to Continue")

        message = (
            "Choose the number to change the configuration state. "
            "1. Set HOME position\n"
            "2. Set negative software limit\n"
            "3. Set positive software limit\n"
            "4. Set encoder increment value\n"
            "5. Use custom command\n"
            "6.Save and Exit Configuration State\n")

        value = int(input("Choose Configuration to Change"))

        if value == 6:
            logger.info("Exiting configuration")
            return
        custom_command = False

        # Enter configuration state
        ret = self.__send_command(cmd='PW1', stage_id=stage_id)

        logger.info(ret)
        while True:

            if value == 0:
                logger.info(message)
                value = int(input("Choose Configuration to Change"))

            if value == 1:      # Set HOME position
                cmd = "HT1"

            elif value == 2:    # Set negative software limit
                cmd = "SL"
                value = input("Enter value between -10^12 to 0")

            elif value == 3:    # Set positive software limit
                cmd = "SR"
                value = input("Enter value between 0 to 10^12")

            elif value == 4:    # Set encoder increment value
                cmd = "SU"
                value = input("Enter value between 10^-6 to 10^12")

            elif value == 5:
                cmd = input("Enter custom command")
                custom_command = True
            elif value == 6:
                # Exit configuration state
                ret = self.__send_command(cmd='PW0', stage_id=stage_id)
                logger.info(ret)
                break
            else:
                logger.info("Value not recognized, exiting config state.")
                ret = self.__send_command(cmd='PW0', stage_id=stage_id)
                logger.info(ret)
                return

            logger.info(ret, value, custom_command)

            # Send command
            ret = self.__send_command(cmd=cmd, stage_id=stage_id,
                                      custom_command=custom_command)
            logger.info(ret)

            # Reset for next round of commands
            value = 0
            logger.info(value)
            time.sleep(3)
            custom_command = False

    # Not Used
    def set_encoder_value(self, value=12.5, stage_id=1):
        """
        Set encoder increment value

        :return:bool, status message
        """
        return self.__send_command(cmd="SU", stage_id=stage_id,
                                   parameters=[value])

    # Not Used
    def get_all(self, stage_id=1):
        """
        Get all axis parameters
        :return: bool, status message
        """
        return self.__send_command(cmd='ZT', stage_id=stage_id)

    # Not Used
    def disable_esp(self, stage_id=1):
        """
        Disable loading stage eeprom data on power up
        :return: bool, status message
        """
        return self.__send_command(cmd='ZX1', stage_id=stage_id)

    # Not Used
    def enable_esp(self, stage_id=1):
        """
        Enable loading stage eeprom data on power up
        :return: bool, status message
        """
        return self.__send_command(cmd='ZX3', stage_id=stage_id)

    # Not Used
    def reset(self, stage_id=1):
        return self.__send_command(cmd="RS", stage_id=stage_id)

    # Not Used
    def get_limits(self, stage_id=1):
        return self.__send_command(cmd="ZT", stage_id=stage_id)

    # Not Used
    def run_manually(self, stage_id=1):
        while True:

            cmd = input("Enter Command")

            if not cmd:
                break

            ret = self.__send_command(cmd=cmd, stage_id=stage_id,
                                      custom_command=True)
            logger.info("End: %s", ret)


if __name__ == "__main__":
    s = Stage()
    # logger.info(s.get_limits(stage_id=2))
    # logger.info(s.get_position(stage_id=1))
    # logger.info(s.disable_esp(1))
    # time.sleep(3)

    # s.run_manually(1)
    # s.enter_config_state(1)
    # logger.info(s.home(1))
    # logger.info(s.reset(1))
    # logger.info(s.enable_esp(1))
    # logger.info(s.set_encoder_value(value=.000244140625, stage_id=1))
    # logger.info(s.get_all(1))
    # time.sleep(5)
    # logger.info(s.home(1))
    # time.sleep(4)
    # logger.info(s.move_focus(.4, stage_id=1))
    # logger.info(s.move_focus(2.5, stage_id=2))
    # logger.info(s.home(1))
    logger.info(s.get_position(2))
    logger.info(s.get_state(1))
    # logger.info(s.home(2))
    # logger.info(s.move_focus(3.5, stage_id=2))
    # time.sleep(1)
    # logger.info(s.get_position(stage_id=2))

    # logger.info(s.home(1))
    # logger.info(s.move_focus(.52, stage_id=1))
    # logger.info(s.get_position(stage_id=1))

    # logger.info(s.home(2))
    # logger.info(s.move_focus(5.0, stage_id=2))
    # logger.info(s.get_position(stage_id=2))
