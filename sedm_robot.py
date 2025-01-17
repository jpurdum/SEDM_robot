import smtplib
import urllib.error
from observatory.server import ocs_client
from sky.server import sky_client
from sanity.server import sanity_client
from utils import sedmHeader, rc_filter_coords, rc_focus
import os
import sys
import json
import datetime
import logging
from logging.handlers import TimedRotatingFileHandler
import time
from threading import Thread
import math
import pprint
import numpy as np
import shutil
import glob
import pandas as pd
import random
import subprocess
from astropy.time import Time
from email.message import EmailMessage
from astropy.coordinates import SkyCoord
import astropy.units as u
from astroquery.mpc import MPC
from astroquery.jplhorizons import Horizons
import astroquery.exceptions
import pickle
from selenium import webdriver
from selenium.webdriver.support.select import Select
from urllib.request import urlopen
from urllib.parse import quote_plus
import SEDM_robot_version as Version

DEF_PROG = '2022B-calib'

with open(os.path.join(Version.CONFIG_DIR, 'logging.json')) as cfg_file:
    log_cfg = json.load(cfg_file)

with open(os.path.join(Version.CONFIG_DIR, 'sedm_robot.json')) as cfg_file:
    sedm_robot_cfg = json.load(cfg_file)

with open(os.path.join(Version.CONFIG_DIR, 'cameras.json')) as cfg_file:
    cam_cfg = json.load(cfg_file)

with open(os.path.join(Version.CONFIG_DIR, 'sedm_observe.json')) as cfg_file:
    sedm_observe_cfg = json.load(cfg_file)

with open(os.path.join(Version.CONFIG_DIR, 'stages.json')) as cfg_file:
    stages_cfg = json.load(cfg_file)

logger = logging.getLogger("sedmLogger")
logger.setLevel(logging.DEBUG)
logging.Formatter.converter = time.gmtime
logHandler = TimedRotatingFileHandler(os.path.join(log_cfg['abspath'],
                                                   'sedm_robot.log'),
                                      when='midnight', utc=True, interval=1,
                                      backupCount=360)

formatter = logging.Formatter("%(asctime)s--%(levelname)s--%(module)s--"
                              "%(funcName)s: %(message)s")
logHandler.setFormatter(formatter)
logHandler.setLevel(logging.DEBUG)
logger.addHandler(logHandler)

console_formatter = logging.Formatter("%(asctime)s: %(message)s")
consoleHandler = logging.StreamHandler(sys.stdout)
consoleHandler.setFormatter(console_formatter)
logger.addHandler(consoleHandler)

status_file_dir = sedm_observe_cfg['status_dir']
status_dict = {}
rc_fastbias_done_file = os.path.join(os.path.join(status_file_dir,
                                                  "rc_fastbias_done.txt"))
status_dict['rc_fastbias'] = rc_fastbias_done_file
rc_slowbias_done_file = os.path.join(os.path.join(status_file_dir,
                                                  "rc_slowbias_done.txt"))
status_dict['rc_slowbias'] = rc_slowbias_done_file
ifu_slowbias_done_file = os.path.join(os.path.join(status_file_dir,
                                                   "ifu_slowbias_done.txt"))
status_dict['ifu_slowbias'] = ifu_slowbias_done_file
rc_domes_done_file = os.path.join(os.path.join(status_file_dir,
                                               "rc_domes_done.txt"))
status_dict['rc_domes'] = rc_domes_done_file
ifu_domes_done_file = os.path.join(os.path.join(status_file_dir,
                                                "ifu_domes_done.txt"))
status_dict['ifu_domes'] = ifu_domes_done_file
lamps_done_file = os.path.join(os.path.join(status_file_dir, "lamps_done.txt"))
status_dict['lamps'] = lamps_done_file
logger.info("Starting Logger: Logger file is %s", 'sedm_robot.log')

# Don't send too many emails!
email_count = 0

def uttime(offset=0):
    if not offset:
        return Time(datetime.datetime.utcnow())
    else:
        return Time(datetime.datetime.utcnow() +
                    datetime.timedelta(seconds=offset))


def send_alert_email(body):
    """
    Send an alert via email to addresses in alertemail.config.json"

    :param body: str - the message to send

    """
    # Read in config file
    with open(os.path.join(Version.CONFIG_DIR, 'alertemail.config.json')) as file:
        alert_cfg = json.load(file)

    # With EmailMessage send alert email
    dt = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    global email_count
    email_count += 1
    if email_count < 11:
        msg = EmailMessage()
        msg['To'] = alert_cfg['mail_list']
        msg['Subject'] = "SEDM-P60 ALERT!"
        msg['From'] = alert_cfg['nemea_email']
        msg.set_content(dt + ":  " + body)

        # local SMTP server
        with smtplib.SMTP("smtp-server.astro.caltech.edu") as send:
            send.send_message(msg)

        logger.info("Email alert!: %s", body)
    else:
        logger.info("Email limit exceeded. Alert!!: %s", body)

def iso_to_epoch(iso_time, epoch_year=False):
    from datetime import datetime
    dt = datetime.fromisoformat(iso_time.replace('T', ' ').replace('Z', ''))
    # Standard astronomical epoch in decimal years
    if epoch_year:
        year_part = dt - datetime(year=dt.year, month=1, day=1)
        year_length = datetime(year=dt.year + 1, month=1, day=1) - \
            datetime(year=dt.year, month=1, day=1)
        ret = dt.year + year_part / year_length
    # P60 TCS required epoch: UT decimal hour
    else:
        ret = dt.hour + ((dt.minute * 60) + dt.second) / 3600.

    return ret


# noinspection PyPep8Naming,PyShadowingNames
class SEDm:
    def __init__(self, observer="SEDm", run_ifu=True, run_rc=True,
                 initialized=False, run_stage=True, run_arclamps=True,
                 run_ocs=True, run_telescope=True, run_sky=True,
                 run_sanity=True, configuration_file='', data_dir=None,
                 focus_temp=None, focus_pos=None, focus_time=None,
                 focus_guess=False, use_winter=False):
        """

        :param observer:
        :param run_ifu:
        :param run_rc:
        :param initialized:
        :param run_stage:
        :param run_arclamps:
        :param run_ocs:
        :param run_telescope:
        :param run_sky:
        :param run_sanity:
        :param configuration_file:
        :param data_dir:
        :param focus_temp:
        :param focus_pos:
        :param focus_guess
        """
        logger.info("Robotic system initializing")
        self.observer = observer
        self.run_ifu = run_ifu
        self.run_rc = run_rc
        self.run_stage = run_stage
        self.run_ocs = run_ocs
        self.run_sky = run_sky
        self.run_sanity = run_sanity
        self.run_arclamps = run_arclamps
        self.run_telescope = run_telescope
        self.use_winter = use_winter
        self.initialized = initialized
        self.data_dir = data_dir
        self.focus_temp = focus_temp
        self.focus_pos = focus_pos
        self.focus_time = focus_time
        self.focus_guess = focus_guess

        self.header = sedmHeader.addHeader()
        self.rc = None
        self.ifu = None
        self.ocs = None
        self.sky = None
        self.sanity = None
        self.winter = None
        self.lamp_dict_status = {'cd': 'off', 'hg': 'off', 'xe': 'off'}
        self.stage_dict = {'ifufocus': -999, 'ifufoc2': -999}
        self.get_tcs_info = True
        self.get_lamp_info = True
        self.get_stage_info = True
        self.p60prpi = 'SEDm'
        self.p60prnm = 'SEDmOBS'
        self.p60prid = '2019B-NoID'
        self.obj_id = -1
        self.req_id = -1
        self.guider_list = []
        self.lamp_wait_time = dict(xe=120, cd=420, hg=120, hal=120)

        self.calibration_id_dict = {
            'bias': 3, 'twilight': 4, 'hal': 5, 'hg': 13, 'xe': 12, 'cd': 14,
            'standard': 15, 'dark': 32, 'GD50': 16, 'SA95-42': 17, 'HZ4': 18, 'LB227': 19,
            'HZ2': 20, 'G191B2B': 21, 'BD+75d325': 22, 'Feige34': 23,
            'HZ44': 24, 'BD+33d2642': 25, 'G24-9': 26, 'BD+28d4211': 27,
            'G93-48': 28, 'BD+25d4655': 29, 'Feige110': 30, 'GD248': 31,
            'focus': {'ifu': 10, 'rc': 6}
        }
        self.telescope_moving_done_path = 'telescope_move_done.txt'
        self.required_sciobs_keywords = ['ra', 'dec', 'name', 'obs_dict']

        if not configuration_file:
            configuration_file = os.path.join(Version.CONFIG_DIR,
                                              'sedm_robot.json')

        with open(configuration_file) as data_file:
            self.params = json.load(data_file)

        self.robot_image_dir = self.params['robot_image_dir']
        self.stow_profiles = self.params['stow_profiles']
        self.ifu_ip = self.params['ifu_ip']
        self.ifu_port = self.params['ifu_port']
        self.rc_ip = self.params['rc_ip']
        self.rc_port = self.params['rc_port']
        self.non_sidereal_dir = self.params['non_sid_dir']
        self.directory_made = False
        self.obs_dir = ""
        self.verbose = False

    def _ut_dir_date(self, offset=0):
        dir_date = (datetime.datetime.utcnow() +
                    datetime.timedelta(days=offset))
        dir_date = dir_date.strftime("%Y%m%d")
        # logger.info("Setting directory date to: %s", dir_date)
        return dir_date

    def initialize(self, lamps_off=False):
        """
        Initialize the system based on the initial conditions set from
        calling SEDm class
        :param lamps_off: bool - set to turn off all cal lamps when initializing
        :return:
        """

        start = time.time()

        logger.info("run_rc = %s", self.run_rc)
        if self.run_rc:
            logger.info("Initializing RC camera")
            try:
                if "pixis" in cam_cfg['rc_driver']:
                    from cameras.server import cam_client as cam_driver
                    logger.info("Using PIXIS driver")
                else:
                    from cameras.server import andor_cam_client as cam_driver
                    logger.info("Using Andor driver")

                self.rc = cam_driver.Camera(self.rc_ip, self.rc_port)
                logger.info('rc return: %s', self.rc.initialize())
            except Exception as e:
                send_alert_email("RC client not set up. "
                                 "Check on Pylos if server is running")
                logger.error("Error setting up RC client, disabling")
                logger.error(str(e))
                self.run_rc = False

        logger.info("run_ifu = %s", self.run_ifu)
        if self.run_ifu:
            logger.info("Initializing IFU camera")
            try:
                if "pixis" in cam_cfg['ifu_driver']:
                    from cameras.server import cam_client as cam_driver
                    logger.info("Using PIXIS driver")
                else:
                    from cameras.server import andor_cam_client as cam_driver
                    logger.info("Using Andor driver")
                self.ifu = cam_driver.Camera(self.ifu_ip, self.ifu_port)
                logger.info('ifu return: %s', self.ifu.initialize())
            except Exception as e:
                send_alert_email("IFU client not set up. "
                                 "Check on Pylos to see if server is running")
                logger.error("Error setting up IFU client, disabling")
                logger.error(str(e))
                self.run_ifu = False

        logger.info("Wait 5 sec")
        time.sleep(5)
        logger.info("Check temperature status")

        if self.run_rc:
            rc_get_temp_status = self.rc.get_temp_status()
            logger.info('rc_get_temp_status: %s', rc_get_temp_status)
            if "error" in rc_get_temp_status:
                logger.error('error: %s', rc_get_temp_status['error'])
                rc_lock = False
            else:
                rc_lock = rc_get_temp_status['templock']
        else:
            rc_lock = True

        if self.run_ifu:
            ifu_get_temp_status = self.ifu.get_temp_status()
            logger.info('ifu_get_temp_status: %s', ifu_get_temp_status)
            if "error" in ifu_get_temp_status:
                logger.error('error: %s', ifu_get_temp_status['error'])
                ifu_lock = False
            else:
                ifu_lock = ifu_get_temp_status['templock']
        else:
            ifu_lock = True

        # loop until locked
        rc_tries = 0
        ifu_tries = 0
        while not rc_lock or not ifu_lock:

            logger.info("Waiting for temperature lock")
            time.sleep(5)

            if self.run_rc:
                rc_get_temp_status = self.rc.get_temp_status()
                if "error" in rc_get_temp_status:
                    logger.error('error: %s', rc_get_temp_status['error'])
                    rc_lock = False
                    rc_temp = 0.
                else:
                    rc_lock = rc_get_temp_status['templock']
                    rc_temp = rc_get_temp_status['camtemp']

                    # Double-check the temperature is indicative of a connected camera server
                    if type(rc_temp) == float:
                        pass
                    elif type(rc_temp) == int:
                        pass
                    else:
                        send_alert_email("RC temperature returned a %s type object. "
                                         "This needs to be a float or integer"
                                         "The RC server is probably disconnected" % type(rc_temp))

                if not rc_lock:
                    rc_tries += 1
                else:
                    rc_tries = 0

                if rc_tries > 180:
                    send_alert_email("RC not achieving temperature lock.")

                logger.info("RC temp, lock = %.1f, %s", rc_temp, rc_lock)

            if self.run_ifu:
                ifu_get_temp_status = self.ifu.get_temp_status()
                if "error" in ifu_get_temp_status:
                    logger.error('error: %s', ifu_get_temp_status['error'])
                    ifu_lock = False
                    ifu_temp = 0.
                else:
                    ifu_lock = ifu_get_temp_status['templock']
                    ifu_temp = ifu_get_temp_status['camtemp']

                    # Double-check the temperature is indicative of a connected camera server
                    if type(ifu_temp) == float:
                        pass
                    elif type(ifu_temp) == int:
                        pass
                    else:
                        send_alert_email("IFU temperature returned a %s type object. "
                                         "This needs to be a float or iteger"
                                         "The RC server is probably disconnected" % type(ifu_temp))

                if not ifu_lock:
                    ifu_tries += 1
                else:
                    ifu_tries = 0

                if ifu_tries > 180:
                    send_alert_email("IFU not achieving temperature lock.")

                logger.info("IFU temp, lock = %.1f, %s", ifu_temp, ifu_lock)

        logger.info("RC and IFU temperature lock achieved")

        if self.run_sky:
            logger.info("Initializing sky server")
            self.sky = sky_client.Sky()

        if self.run_ocs:
            logger.info("Initializing observatory components")
            self.ocs = ocs_client.Observatory()

            if self.run_arclamps and self.run_stage and self.run_telescope:
                logger.info('ocs return: %s', self.ocs.initialize_ocs())
                logger.info(self.ocs.take_control())
                if lamps_off:
                    logger.info("Turning arc lamps off")
                    for lamp in ['xe', 'cd', 'hg']:
                        self.ocs.arclamp(lamp, command="OFF")
                        logger.info(lamp)
                        time.sleep(1)
                    logger.info("Turning halogen lamp off")
                    self.ocs.halogens_off()
            else:
                if self.run_arclamps:
                    self.ocs.initialize_lamps()
                    if lamps_off:
                        logger.info("Turning arc lamps off")
                        for lamp in ['xe', 'cd', 'hg']:
                            self.ocs.arclamp(lamp, command="OFF")
                            logger.info(lamp)
                            time.sleep(1)
                if self.run_stage:
                    self.ocs.initialize_stages()
                if self.run_telescope:
                    self.ocs.initialize_tcs()
                    if lamps_off:
                        logger.info("Turning halogen lamp off")
                        self.ocs.halogens_off()

        if self.run_sanity:
            logger.info("Initializing sanity server")
            self.sanity = sanity_client.Sanity()

        if self.use_winter:
            logger.info("Initializing connection to WINTER for weather info")
            from observatory.telescope import winter
            self.winter = winter.Winter()

        self.initialized = True
        return {'elaptime': time.time() - start, 'data': "System initialized"}

    def get_status_dict(self, do_lamps=True, do_stages=True):
        stat_dict = {}

        # Are we NOT running the ocs?
        if self.ocs is None:
            pass
        # We ARE running the ocs
        else:
            # First try at position
            ret = self.ocs.check_pos()
            if 'data' in ret:
                sd = ret['data']
                if isinstance(sd, dict):
                    stat_dict.update(sd)
                else:
                    logger.warning("Bad ?POS return type: %s", sd)
                    # Second try
                    ret = self.ocs.check_pos()
                    if 'data' in ret:
                        sd = ret['data']
                        if isinstance(sd, dict):
                            stat_dict.update(sd)
                        else:
                            logger.warning("Bad ?POS return(2): %s", sd)
                    else:
                        logger.error("Could not get ?POS return: %s", ret)
            else:
                logger.error("Bad ?POS return: %s", ret)
            try:
                stat_dict.update(self.ocs.check_weather()['data'])
                if self.use_winter:
                    wret = self.winter.get_weather()
                    if 'data' in wret:
                        win_dict = wret['data']
                        # stat_dict['weather_status'] = win_dict[
                        #   'Weather_Status']
                        stat_dict['windspeed_average'] = win_dict[
                            'Average_Wind_Speed']
                        stat_dict['wind_dir_current'] = win_dict[
                            'Wind_Direction']
                        stat_dict['outside_air_temp'] = win_dict['Outside_Temp']
                        stat_dict['inside_air_temp'] = win_dict['Outside_Temp']
                        stat_dict['outside_rel_hum'] = win_dict['Outside_RH']
                        stat_dict['inside_rel_hum'] = win_dict['Outside_RH']
                        stat_dict['outside_dewpt'] = win_dict[
                            'Outside_Dewpoint']
                        stat_dict['inside_dewpt'] = win_dict['Outside_Dewpoint']
                sret = self.ocs.check_status()
                if 'data' in sret:
                    sd = sret['data']
                    if isinstance(sd, dict):
                        stat_dict.update(sd)
                    else:
                        logger.warning('Bad ocs.check_status return type:\n%s',
                                       sd)
                        sret = self.ocs.check_status()
                        if 'data' in sret:
                            sd = sret['data']
                            if isinstance(sd, dict):
                                stat_dict.update(sd)
                            else:
                                logger.warning(
                                    'Bad ocs.check_status return(2):\n%s', sd)
                        else:
                            logger.error("Could not get ocs.check_status "
                                         "return: %s", sret)
                else:
                    logger.error("Bad ocs.check_status return: %s", sret)
                # stat_dict.update(self.ocs.check_status()['data'])
            except Exception as e:
                logger.error(str(e))
                pass

            if do_lamps and self.run_arclamps:

                lret = self.ocs.arclamp('xe', 'status', force_check=True)
                if 'data' in lret:
                    ld = lret['data']
                    if isinstance(ld, str):
                        stat_dict['xe_lamp'] = ld
                    else:
                        stat_dict['xe_lamp'] = 'UNKNOWN'
                else:
                    stat_dict['xe_lamp'] = 'UNKNOWN'
                self.lamp_dict_status['xe'] = stat_dict['xe_lamp']

                lret = self.ocs.arclamp('cd', 'status', force_check=True)
                if 'data' in lret:
                    ld = lret['data']
                    if isinstance(ld, str):
                        stat_dict['cd_lamp'] = ld
                    else:
                        stat_dict['cd_lamp'] = 'UNKNOWN'
                else:
                    stat_dict['cd_lamp'] = 'UNKNOWN'
                self.lamp_dict_status['cd'] = stat_dict['cd_lamp']

                lret = self.ocs.arclamp('hg', 'status', force_check=True)
                if 'data' in lret:
                    ld = lret['data']
                    if isinstance(ld, str):
                        stat_dict['hg_lamp'] = ld
                    else:
                        stat_dict['hg_lamp'] = 'UNKNOWN'
                else:
                    stat_dict['hg_lamp'] = 'UNKNOWN'
                self.lamp_dict_status['hg'] = stat_dict['hg_lamp']

            else:
                stat_dict['xe_lamp'] = self.lamp_dict_status['xe']
                stat_dict['cd_lamp'] = self.lamp_dict_status['cd']
                stat_dict['hg_lamp'] = self.lamp_dict_status['hg']

            if do_stages and self.run_stage:
                stat_dict['ifufocus'] = self.ocs.stage_position(1)['data']
                self.stage_dict['ifufocus'] = stat_dict['ifufocus']
                stat_dict['ifufoc2'] = self.ocs.stage_position(2)['data']
                self.stage_dict['ifufoc2'] = stat_dict['ifufoc2']
            else:
                stat_dict['ifufocus'] = self.stage_dict['ifufocus']
                stat_dict['ifufoc2'] = self.stage_dict['ifufoc2']
        return stat_dict

    def take_image(self, cam, exptime=0, shutter='normal', readout=2.0,
                   start=None, save_as=None, test='', imgtype='NA',
                   objtype='NA', object_ra=None, object_dec=None, email='',
                   p60prid='NA', p60prpi='SEDm', p60prnm='',
                   obj_id=-999, req_id=-999, objfilter='NA', imgset='NA',
                   is_rc=True, abpair=False, name='Unknown', do_lamps=True,
                   do_stages=True, verbose=False):
        """

        :param do_stages:
        :param do_lamps:
        :type object_ra: object
        :param exptime:
        :param cam:
        :param shutter:
        :param readout:
        :param name:
        :param start:
        :param save_as:
        :param test:
        :param imgtype:
        :param objtype:
        :param object_ra:
        :param object_dec:
        :param email:
        :param p60prid:
        :param p60prpi:
        :param p60prnm:
        :param obj_id:
        :param req_id:
        :param objfilter:
        :param imgset:
        :param is_rc:
        :param abpair:
        :param verbose:
        :return:
        """
        if verbose:
            pass
        # Timekeeping
        if not start:
            start = time.time()
        # logger.info("Preparing to take an image")
        # Make sure the image directory exists on local host
        if not save_as:
            if not self.directory_made:
                self.obs_dir = os.path.join(self.robot_image_dir,
                                            self._ut_dir_date())
                if not os.path.exists(os.path.join(self.robot_image_dir,
                                                   self._ut_dir_date())):
                    os.mkdir(os.path.join(self.robot_image_dir,
                                          self._ut_dir_date()))
                    self.directory_made = True
        obsdict = {'starttime': start}

        readout_end = (datetime.datetime.utcnow()
                       + datetime.timedelta(seconds=exptime))

        # 1. Start the exposure and return back to the prompt
        ret = cam.take_image(shutter=shutter, exptime=exptime,
                             readout=readout, save_as=save_as,
                             return_before_done=True)

        if not is_rc:
            logger.info("IFU cam.take_image status:\n%s", ret)

        # 2. Get the TCS information for the conditions at the start of the
        # exposure
        begin_tcs_dict = self.get_status_dict(do_stages=do_stages,
                                              do_lamps=do_lamps)
        if not is_rc:
            logger.info("updating IFU beginning TCS keywords:\n%s" %
                        str(begin_tcs_dict))
        obsdict.update(begin_tcs_dict)

        if not object_ra or not object_dec:
            logger.info("Using TCS RA and DEC")
            try:
                object_ra = obsdict['telescope_ra']
                object_dec = obsdict['telescope_dec']
            except KeyError:
                logger.warning("No TCS object coords in keywords!")
                object_ra = 0.0
                object_dec = 0.0

        project_dict = self.header.set_project_keywords(
            test=test, imgtype=imgtype, objtype=objtype,
            object_ra=object_ra, object_dec=object_dec,
            email=email, name=name, p60prid=p60prid,
            p60prpi=p60prpi, p60prnm=p60prnm, obj_id=obj_id,
            req_id=req_id, objfilter=objfilter, imgset=imgset,
            is_rc=is_rc, abpair=abpair)

        if not is_rc:
            logger.info("updating IFU project keywords:\n%s" %
                        str(project_dict))
        obsdict.update(project_dict)

        while datetime.datetime.utcnow() < readout_end:
            time.sleep(.01)

        end_tcs_dict = self.get_status_dict(do_lamps=False, do_stages=False)

        if not is_rc:
            logger.info("updating IFU end of obs TCS keywords:\n%s" %
                        str(end_tcs_dict))
        obsdict.update(self.header.prep_end_header(end_tcs_dict))

        # logger.info("Reconnecting now")
        try:
            ret = cam.listen()
            if not is_rc:
                logger.info("cam.listen status:\n%s", ret)
        except Exception as e:
            logger.error("unable to listen for new image", exc_info=True)
            logger.error("Error waiting for the file to write out")
            logger.error(str(e))
            ret = None

        # TODO: add obsdict verification routine
        if isinstance(ret, dict) and 'data' in ret:
            if not is_rc:
                logger.info("Adding the IFU header")
            self.header.set_header(ret['data'], obsdict)
            return ret
        else:
            logger.warning("There was no return: %s", ret)

            # This is a test to see if last image failed to write or the
            # connection timed out.
            # * means all if we need specific format then *.csv
            list_of_files = glob.glob('/home/sedm/images/%s/*.fits' %
                                      datetime.datetime.utcnow().strftime(
                                          "%Y%m%d"))
            latest_file = max(list_of_files, key=os.path.getctime)

            logger.info('Checking latest file: %s' % latest_file)
            base_file = os.path.basename(latest_file)
            if 'ifu' in base_file:
                fdate = datetime.datetime.strptime(base_file,
                                                   "ifu%Y%m%d_%H_%M_%S.fits")
            else:
                fdate = datetime.datetime.strptime(base_file,
                                                   "rc%Y%m%d_%H_%M_%S.fits")

            start_time = readout_end - datetime.timedelta(seconds=exptime)
            fdate += datetime.timedelta(seconds=1)
            diff = (fdate - start_time).seconds

            # Re-establish the camera connection just to make sure the
            # issue isn't with them
            logger.info(self.initialize())

            if diff < 10:
                logger.info("Add the header")
                logger.info(self.header.set_header(latest_file, obsdict))
                return {'elaptime': time.time()-start, 'data': latest_file}
            else:
                if is_rc:
                    send_alert_email("Last RC Image failed to write")
                else:
                    send_alert_email("Last IFU Image failed to write")
                logger.warning("File not a match, saving header info")
                save_path = os.path.join(
                    self.obs_dir, "header_dict_" +
                                  start_time.strftime("%Y%m%d_%H_%M_%S"))
                f = open(save_path, "wb")
                pickle.dump(dict, f)
                f.close()
                return {
                    "elaptime": time.time()-start,
                    "error": "Camera not returned",
                    "data": "header file saved to %s" % save_path
                }

    def take_bias(self, cam, N=1, startN=1, shutter='closed', readout=2.0,
                  generate_request_id=True, name='', save_as=None, test='',
                  req_id=-999):
        """

        :param req_id:
        :param readout:
        :param cam:
        :param test:
        :param save_as:
        :param N:
        :param shutter:
        :param startN:
        :param generate_request_id:
        :param name:
        :return:
        """
        # Pause condition to keep the IFU and RC cameras out of sync
        # during calibrations.  Does not effect the efficiency of the
        # system as a whole
        time.sleep(2)

        img_list = []
        start = time.time()

        if not name:
            name = 'bias'

        obj_id = self.calibration_id_dict['bias']

        if generate_request_id:
            ret = self.sky.get_calib_request_id(camera=cam.prefix()['data'],
                                                N=N, exptime=0,
                                                object_id=obj_id)
            logger.info("sky.get_calib_request_id status:\n%s", ret)
            if "data" in ret:
                req_id = ret['data']

        for img in range(startN, N + 1, 1):
            logger.info("%d, %d", img, N)
            if N != startN:
                start = time.time()
                do_stages = False
                do_lamps = False
            else:
                do_stages = True
                do_lamps = True
            namestr = "%s %s of %s" % (name, img, N)
            ret = self.take_image(cam, shutter=shutter, readout=readout,
                                  name=namestr, start=start, test=test,
                                  save_as=save_as, imgtype='bias',
                                  objtype='Calibration', exptime=0,
                                  object_ra="", object_dec="", email='',
                                  p60prid=DEF_PROG, p60prpi='SEDm',
                                  p60prnm='SEDm Calibration File',
                                  obj_id=obj_id, req_id=req_id,
                                  objfilter='NA', imgset='NA',
                                  do_stages=do_stages, do_lamps=do_lamps,
                                  is_rc=True, abpair=False)
            logger.info("take_image(BIAS) status:\n%s", ret)

            if 'error' in ret:
                logger.error("Bad image: error in return")
            elif 'data' in ret:
                img_list.append(ret['data'])
            else:
                logger.error("Bad image: no return")

        if generate_request_id:
            sky_ret = self.sky.update_target_request(req_id, status="COMPLETED")
            logger.info("sky.update_target_request status:\n%s", sky_ret)

        return {'elaptime': time.time() - start, 'data': img_list}

    def take_dark(self, cam, N=1, startN=1, shutter='closed', exptime=1800, readout=2.0,
                  generate_request_id=True, name='', save_as=None, test='',
                  req_id=-999):
        """

        :param req_id:
        :param readout:
        :param exptime:
        :param cam:
        :param test:
        :param save_as:
        :param N:
        :param shutter:
        :param startN:
        :param generate_request_id:
        :param name:
        :return:
        """
        # Pause condition to keep the IFU and RC cameras out of sync
        # during calibrations.  Does not effect the efficiency of the
        # system as a whole
        time.sleep(2)

        img_list = []
        start = time.time()

        if not name:
            name = 'dark'

        obj_id = self.calibration_id_dict['dark']

        if generate_request_id:
            ret = self.sky.get_calib_request_id(camera=cam.prefix()['data'],
                                                N=N, exptime=exptime,
                                                object_id=obj_id)
            logger.info("sky.get_calib_request_id status:\n%s", ret)
            if "data" in ret:
                req_id = ret['data']

        for img in range(startN, N + 1, 1):
            logger.info("%d, %d", img, N)
            if N != startN:
                start = time.time()
                do_stages = False
                do_lamps = False
            else:
                do_stages = True
                do_lamps = True
            namestr = "%s %s of %s" % (name, img, N)
            ret = self.take_image(cam, shutter=shutter, readout=readout,
                                  name=namestr, start=start, test=test,
                                  save_as=save_as, imgtype='dark',
                                  objtype='Calibration', exptime=exptime,
                                  object_ra="", object_dec="", email='',
                                  p60prid=DEF_PROG, p60prpi='SEDm',
                                  p60prnm='SEDm Calibration File',
                                  obj_id=obj_id, req_id=req_id,
                                  objfilter='NA', imgset='NA',
                                  do_stages=do_stages, do_lamps=do_lamps,
                                  is_rc=True, abpair=False)
            logger.info("take_image(Dark) status:\n%s", ret)

            if 'error' in ret:
                logger.error("Bad image: error in return")
            elif 'data' in ret:
                img_list.append(ret['data'])
            else:
                logger.error("Bad image: no return")

        if generate_request_id:
            sky_ret = self.sky.update_target_request(req_id, status="COMPLETED")
            logger.info("sky.update_target_request status:\n%s", sky_ret)

        return {'elaptime': time.time() - start, 'data': img_list}

    def take_dome(self, cam, N=1, exptime=180, readout=2.0,
                  do_lamp=True, wait=True, obj_id=None,
                  shutter='normal', name='', test='',
                  move=False, ha=3.6, dec=50, domeaz=40,
                  save_as=None, req_id=-999,
                  startN=1, generate_request_id=True):
        """

        :param cam:
        :param N:
        :param exptime:
        :param readout:
        :param do_lamp:
        :param wait:
        :param obj_id:
        :param shutter:
        :param name:
        :param test:
        :param move:
        :param ha:
        :param dec:
        :param domeaz:
        :param save_as:
        :param req_id:
        :param startN:
        :param generate_request_id:
        :return:
        """
        time.sleep(2)
        start = time.time()  # Start the clock on the observation
        # 1. Check if the image type is calibration type and set the tracking
        #    list if so
        if not obj_id:
            obj_id = self.calibration_id_dict['hal']

        if generate_request_id:
            ret = self.sky.get_calib_request_id(camera=cam.prefix()['data'],
                                                N=N, exptime=0,
                                                object_id=obj_id)
            logger.info("sky.get_calib_request_id status:\n%s", ret)

            if "data" in ret:
                req_id = ret['data']
        # 2. Move the telescope to the calibration stow position
        if move:
            ret = self.ocs.stow(ha=ha, dec=dec, domeaz=domeaz)
            logger.info("ocs.stow(dome) status:\n%s", ret)
            # 3a. Check that we made it to the calibration stow position
            # TODO: Implement return checking of OCS returns
            if not ret:
                send_alert_email("Unable to move telescope to stow position")
                return "Unable to move telescope to stow position"

        # 3. Turn on the lamps and wait for them to stabilize
        if do_lamp:
            self.ocs.halogens_on()

        if wait:
            logger.info("Waiting %s seconds for dome lamps to warm up",
                        self.lamp_wait_time['hal'])
            time.sleep(self.lamp_wait_time['hal'])

        if not name:
            name = 'dome lamp'

        # 4. Start the observations
        for img in range(startN, N + 1, 1):

            # 5a. Set the image header keyword name
            logger.info("%d, %d", img, N)
            if N != startN:
                start = time.time()

            namestr = "%s %s of %s" % (name, img, N)
            ret = self.take_image(cam, shutter=shutter, readout=readout,
                                  name=namestr, start=start, test=test,
                                  save_as=save_as, imgtype='dome',
                                  objtype='Calibration', exptime=exptime,
                                  object_ra="", object_dec="", email='',
                                  p60prid=DEF_PROG, p60prpi='SEDm',
                                  p60prnm='SEDm Calibration File',
                                  obj_id=obj_id, req_id=req_id,
                                  objfilter='NA', imgset='NA',
                                  is_rc=True, abpair=False)
            logger.info("take_image(dome) status:\n%s", ret)

        if do_lamp:
            self.ocs.halogens_off()

        if generate_request_id:
            sky_ret = self.sky.update_target_request(req_id, status="COMPLETED")
            logger.info("sky.update_target_request status:\n%s", sky_ret)

    def take_arclamp(self, cam, lamp, N=1, exptime=1, readout=2.0,
                     do_lamp=True, wait=True, obj_id=None,
                     shutter='normal', name='', test='',
                     ha=3.6, dec=50.0, domeaz=40,
                     move=True, save_as=None, req_id=-999,
                     startN=1, generate_request_id=True):
        """

        :param cam:
        :param lamp:
        :param N:
        :param exptime:
        :param readout:
        :param do_lamp:
        :param wait:
        :param obj_id:
        :param shutter:
        :param name:
        :param test:
        :param ha:
        :param dec:
        :param domeaz:
        :param move:
        :param save_as:
        :param req_id:
        :param startN:
        :param generate_request_id:
        :return:
        """

        start = time.time()  # Start the clock on the observation

        # Hack to get the naming convention exactly right for the pipeline
        if not name:
            name = lamp[0].upper() + lamp[-1].lower()

        # 1. Check if the image type is calibration type and set the tracking
        #    list if so
        if not obj_id:
            obj_id = self.calibration_id_dict[lamp.lower()]

        if generate_request_id:
            ret = self.sky.get_calib_request_id(camera=cam.prefix()['data'],
                                                N=N, exptime=0,
                                                object_id=obj_id)
            logger.info("sky.get_calib_request_id status:\n%s", ret)

            if "data" in ret:
                req_id = ret['data']

        # 2. Move the telescope to the calibration stow position
        if move:
            ret = self.ocs.stow(ha=ha, dec=dec, domeaz=domeaz)
            logger.info("ocs.stow(arc) status:\n%s", ret)
            # 3a. Check that we made it to the calibration stow position
            # TODO: Implement return checking of OCS returns
            # if not ret:
            #    return "Unable to move telescope to stow position"

        # 3. Turn on the lamps and wait for them to stabilize
        if do_lamp:
            ret = self.ocs.arclamp(lamp, command="ON")
            logger.info("ocs.arclamp status:\n%s", ret)
        if wait:
            logger.info("Waiting %s seconds for %s lamp to warm up",
                        self.lamp_wait_time[lamp.lower()], lamp)
            time.sleep(self.lamp_wait_time[lamp.lower()])

        if not name:
            name = lamp

        # 4. Start the observations
        for img in range(startN, N + 1, 1):

            logger.info("%d, %d", img, N)
            # 5a. Set the image header keyword name
            if N != startN:
                start = time.time()

            namestr = "%s %s of %s" % (name, img, N)
            ret = self.take_image(cam, shutter=shutter, readout=readout,
                                  name=namestr, start=start, test=test,
                                  save_as=save_as, imgtype='lamp',
                                  objtype='Calibration', exptime=exptime,
                                  object_ra="", object_dec="", email='',
                                  p60prid=DEF_PROG, p60prpi='SEDm',
                                  p60prnm='SEDm Calibration File',
                                  obj_id=obj_id, req_id=req_id,
                                  objfilter='NA', imgset='NA',
                                  is_rc=False, abpair=False)
            logger.info("take_image(arc) status:\n%s", ret)

        if do_lamp:
            ret = self.ocs.arclamp(lamp, command="OFF")
            logger.info("ocs.arclamp status:\n%s", ret)

        if generate_request_id:
            sky_ret = self.sky.update_target_request(req_id, status="COMPLETED")
            logger.info("sky.update_target_request status:\n%s", sky_ret)

    def take_twilight(self, cam, N=1, exptime=30, readout=0.1,
                      do_lamp=True, wait=True, obj_id=None,
                      shutter='normal', name='', test='',
                      ra=3.6, dec=50.0, end_time=None,
                      get_focus_coords=True, use_sun_angle=True,
                      max_angle=-11, min_angle=-5, max_time=100,
                      move=True, save_as=None, req_id=-999,
                      startN=1, generate_request_id=True):
        """

        :param cam:
        :param N:
        :param exptime:
        :param readout:
        :param do_lamp:
        :param wait:
        :param obj_id:
        :param shutter:
        :param name:
        :param test:
        :param ra:
        :param dec:
        :param end_time:
        :param get_focus_coords:
        :param use_sun_angle:
        :param max_angle:
        :param min_angle:
        :param max_time:
        :param move:
        :param save_as:
        :param req_id:
        :param startN:
        :param generate_request_id:
        :return:
        """

        # unused parameters
        if do_lamp or wait:
            pass
        if max_angle > min_angle:
            pass
        if startN > 1:
            pass

        start = time.time()  # Start the clock on the observation

        # Hack to get the naming convention exactly right for the pipeline
        if not name:
            name = "Twilight"

        # 1. Check if the image type is calibration type and set the tracking
        #    list if so
        if not obj_id:
            obj_id = self.calibration_id_dict["twilight"]

        if generate_request_id:
            ret = self.sky.get_calib_request_id(camera=cam.prefix()['data'],
                                                N=N, exptime=0,
                                                object_id=obj_id)
            logger.info("sky.get_calib_request_id status:\n%s", ret)

            if "data" in ret:
                req_id = ret['data']

        # 2. Move the telescope to the calibration stow position
        if move:
            stat = self.ocs.check_status()

            if 'data' in stat:
                ret = stat['data']['dome_shutter_status']
                if 'closed' in ret.lower():
                    logger.info("Opening dome")
                    logger.info(self.ocs.dome("open"))
                else:
                    logger.info("Dome open skipping")

            if get_focus_coords:
                ret = self.sky.get_focus_coords()
                logger.info('sky.get_focus_coords status: %s', ret)
                if 'data' in ret:
                    ra = ret['data']['ra']
                    dec = ret['data']['dec']

            ret = self.ocs.tel_move(name=name, ra=ra,
                                    dec=dec)

            if 'data' not in ret:
                logger.warning(ret)

        n = 1
        # 4. Start the observations
        while time.time() - start < max_time:
            if use_sun_angle:
                ret = self.sky.get_twilight_exptime()
                logger.info("sky.get_twilight_exptime status:\n%s", ret)

                if 'data' in ret:
                    exptime = ret['data']['exptime']

                if n != 1:
                    start = time.time()
                    do_stages = False
                    do_lamps = False
                else:
                    do_stages = True
                    do_lamps = True

                namestr = name + ' ' + str(N)
                if end_time:
                    ctime = datetime.datetime.utcnow()
                    etime = ctime + datetime.timedelta(seconds=exptime+50)
                    if Time(etime) > end_time:
                        break
                ret = self.take_image(cam, shutter=shutter, readout=readout,
                                      name=namestr, start=start, test=test,
                                      save_as=save_as, imgtype='twilight',
                                      objtype='Calibration', exptime=exptime,
                                      object_ra="", object_dec="", email='',
                                      p60prid=DEF_PROG, p60prpi='SEDm',
                                      p60prnm='SEDm Calibration File',
                                      obj_id=obj_id, req_id=req_id,
                                      do_stages=do_stages,
                                      do_lamps=do_lamps,
                                      objfilter='NA', imgset='NA',
                                      is_rc=True, abpair=False)
                logger.info("take_image(twi) status:\n%s", ret)
                if move:
                    off = random.random()
                    if off >= .5:
                        sign = -1
                    else:
                        sign = 1

                    ra_off = sign * off * 20
                    dec_off = sign * off * 20
                    if ra_off > dec_off:
                        pass

                    self.ocs.tel_offset(0, -15)

                    self.ocs.tel_offset()
                n += 1

        if generate_request_id:
            sky_ret = self.sky.update_target_request(req_id, status="COMPLETED")
            logger.info("sky.update_target_request status:\n%s", sky_ret)

    def take_datacube(self, cam, cube='ifu', check_for_previous=True,
                      custom_file='', move=False, ha=None, dec=None,
                      domeaz=None, make_files=False):
        """

        :param move:
        :param ha:
        :param dec:
        :param domeaz:
        :param check_for_previous:
        :param custom_file:
        :param cam:
        :param cube:
        :param make_files:
        :return:
        """
        start = time.time()
        if custom_file:
            with open(custom_file) as data_file:
                cube_params = json.load(data_file)
        else:
            cube_params = self.params

        cube_type = "%s" % cube
        logger.info("cube_type  : %s", cube_type)
        logger.info("cube_params: %s", cube_params)
        data_dir = os.path.join(self.robot_image_dir, self._ut_dir_date())

        if move:
            if not ha:
                ha = self.stow_profiles['calibrations']['ha']
            if not dec:
                dec = self.stow_profiles['calibrations']['dec']
            if not domeaz:
                domeaz = self.stow_profiles['calibrations']['domeaz']

            self.ocs.stow(ha=ha, dec=dec, domeaz=domeaz)

        if 'fast_bias' in cube_params[cube_type]['order']:
            N = cube_params[cube_type]['fast_bias']['N']
            rdo = cube_params[cube_type]['fast_bias']['readout']
            files_completed = 0
            if check_for_previous:
                ret = self.sanity.check_for_files(camera=cube,
                                                  keywords={'imgtype': 'bias',
                                                            'adcspeed': rdo},
                                                  data_dir=data_dir)
                if 'data' in ret:
                    files_completed = int(ret['data'])

            if files_completed >= N or os.path.exists(status_dict['%s_fastbias'
                                                                  % cube]):
                logger.info("%s Fast biases already done" % cube.upper())
            else:
                N = N - files_completed
                logger.info("Taking %d fast biases for %s", N, cube)
                self.take_bias(cam, N=N, readout=rdo)
                if make_files:
                    with open(status_dict['%s_fastbias' % cube], 'w') as file:
                        file.write('%s fast biases completed:%s' %
                                   (cube.upper(), uttime()))

        if 'slow_bias' in cube_params[cube_type]['order']:
            N = cube_params[cube_type]['slow_bias']['N']
            rdo = cube_params[cube_type]['slow_bias']['readout']
            files_completed = 0
            if check_for_previous:
                ret = self.sanity.check_for_files(camera=cube,
                                                  keywords={'imgtype': 'bias',
                                                            'adcspeed': rdo},
                                                  data_dir=data_dir)
                if 'data' in ret:
                    files_completed = int(ret['data'])

            if files_completed >= N or os.path.exists(status_dict['%s_slowbias'
                                                                  % cube]):
                logger.info("%s Slow biases already done" % cube.upper())
            else:
                N = N - files_completed
                logger.info("Taking %d slow biases for %s", N, cube)
                self.take_bias(cam, N=N, readout=rdo)
                if make_files:
                    with open(status_dict['%s_slowbias' % cube], 'w') as file:
                        file.write('%s slow biases completed:%s' %
                                   (cube.upper(), uttime()))

        if 'dark' in cube_params[cube_type]['order']:
            N = cube_params[cube_type]['dark']['N']
            rdo = cube_params[cube_type]['dark']['readout']
            exptime = cube_params[cube_type]['dark']['exptime']
            files_completed = 0
            if check_for_previous:
                ret = self.sanity.check_for_files(camera=cube,
                                                  keywords={'imgtype': 'dark',
                                                            'adcspeed': rdo},
                                                  data_dir=data_dir)
                if 'data' in ret:
                    files_completed = int(ret['data'])

            if files_completed >= N or os.path.exists(status_dict['%s_dark'
                                                                  % cube]):
                logger.info("%s Darks already done" % cube.upper())
            else:
                N = N - files_completed
                logger.info("Taking %d Dark images with exposure time %s for %s", N, exptime, cube)
                self.take_dark(cam, N=N, exptime=exptime, readout=rdo)
                if make_files:
                    with open(status_dict['%s_dark' % cube], 'w') as file:
                        file.write('%s Dark images completed:%s' %
                                   (cube.upper(), uttime()))

        if 'dome' in cube_params[cube_type]['order']:
            N = cube_params[cube_type]['dome']['N']
            files_completed = 0
            if check_for_previous:
                if 'ifu' in cube_type:
                    ret = self.sanity.check_for_files(
                            camera=cube,
                            keywords={'imgtype': 'dome', 'adcspeed': 1.0},
                            data_dir=data_dir)
                else:
                    ret = self.sanity.check_for_files(
                            camera=cube,
                            keywords={'imgtype': 'dome', 'adcspeed': 2.0},
                            data_dir=data_dir)
                if 'data' in ret:
                    files_completed = int(ret['data'])

            if files_completed >= N or os.path.exists(status_dict['%s_domes'
                                                                  % cube]):
                logger.info("%s Domes already taken" % cube.upper())
            else:
                N = N - files_completed
                logger.info("Taking %d %s dome flats in each set", N, cube)
                logger.info("Turning on Halogens")
                self.ocs.halogens_on()
                logger.info("Waiting 120 seconds for Halogens to warm up")
                time.sleep(120)
                for i in cube_params[cube_type]['dome']['readout']:
                    logger.info("Readout speed: %s", i)
                    for j in cube_params[cube_type]['dome']['exptime']:
                        logger.info("Taking %d images with exptime(s) %s:",
                                    N, j)
                        self.take_dome(cam, N=N, readout=i, do_lamp=False,
                                       wait=False, exptime=j, move=False)
                if make_files:
                    with open(status_dict['%s_domes' % cube], 'w') as file:
                        file.write('%s domes completed:%s' % (cube.upper(),
                                                              uttime()))
                logger.info("Turning off Halogens")
                self.ocs.halogens_off()

        if os.path.exists(status_dict['lamps']):
            logger.info("Arc Lamps already taken")
        else:
            for lamp in ['hg', 'xe', 'cd']:
                if lamp in cube_params[cube_type]['order']:
                    N = cube_params[cube_type][lamp]['N']
                    rdo = cube_params[cube_type][lamp]['readout']
                    exptime = cube_params[cube_type][lamp]['exptime']
                    logger.info("Taking %d %s arcs for %s", N, lamp, cube)
                    self.take_arclamp(cam, lamp, N=N, readout=rdo, move=False,
                                      exptime=exptime)
                if make_files:
                    with open(status_dict['lamps'], 'a') as file:
                        file.write('%s arclamps completed:%s\n' %
                                   (lamp.capitalize(), uttime()))

        return {'elaptime': time.time() - start, 'data': '%s complete' %
                                                         cube_type}

    def take_datacube_eff(self, custom_file='', move=True,
                          ha=None, dec=None, domeaz=None):
        """

        :param move:
        :param ha:
        :param dec:
        :param domeaz:
        :param custom_file:
        :return:
        """
        start = time.time()

        skip_next = False

        if not self.run_rc and not self.run_ifu:
            send_alert_email("One or Both cameras not active")
            logger.error("Both cameras have to be active")
            return {'elaptime': time.time() - start,
                    'error': 'Efficiency cube mode can only '
                             'be run with both cameras active'}

        if custom_file:
            with open(custom_file) as data_file:
                cube_params = json.load(data_file)
        else:
            cube_params = self.params

        logger.info(cube_params)

        if move:
            if not ha:
                ha = self.stow_profiles['calibrations']['ha']
            if not dec:
                dec = self.stow_profiles['calibrations']['dec']
            if not domeaz:
                domeaz = self.stow_profiles['calibrations']['domeaz']

            self.ocs.stow(ha=ha, dec=dec, domeaz=domeaz)

        # Start by turning on the Cd lamp:
        logger.info("Turning on Cd Lamp")
        ret = self.ocs.arclamp('cd', command="ON")
        logger.info("CD ON: %s", ret)
        ret = self.ocs.arclamp('cd', 'status', force_check=True)['data']

        if 'on' not in ret:
            skip_next = True

        lamp_start = time.time()

        # Now take the biases while waiting for things to finish

        # Start the RC biases in the background
        N_rc = cube_params['rc']['fast_bias']['N']
        t = Thread(target=self.take_bias, kwargs={'cam': self.rc,
                                                  'N': N_rc,
                                                  'readout': 2.0,
                                                  })
        t.daemon = True
        t.start()

        # Wait 5s to start the IFU calibrations so they finish last
        time.sleep(5)
        N_ifu = cube_params['ifu']['fast_bias']['N']
        self.take_bias(self.ifu, N=N_ifu, readout=1.0)

        # Start the RC biases in the background
        N_rc = cube_params['rc']['fast_bias']['N']
        t = Thread(target=self.take_bias, kwargs={'cam': self.rc,
                                                  'N': N_rc,
                                                  'readout': .1,
                                                  })
        t.daemon = True
        t.start()

        # Wait 5s to start the IFU calibrations so they finish last
        # time.sleep(5)
        # N_ifu = cube_params['ifu']['fast_bias']['N']
        # self.take_bias(self.ifu, N=N_ifu, readout=.1)

        # Make sure that we have waited long enough for the 'Cd' lamp to warm
        while time.time() - lamp_start < self.lamp_wait_time['cd']:
            time.sleep(5)

        # Start the 'cd' lamps
        if not skip_next:
            N_cd = cube_params['ifu']['cd']['N']
            exptime = cube_params['ifu']['cd']['exptime']
            self.take_arclamp(self.ifu, 'cd', wait=False, do_lamp=False, N=N_cd,
                              readout=1.0, move=False, exptime=exptime)

            # Turn the lamps off
            ret = self.ocs.arclamp('cd', command="OFF")

            logger.info("CD OFF: %s", ret)
        else:
            _ = self.ocs.arclamp('cd', command="OFF")
            # skip_next = False

        # Move onto to the dome lamp
        logger.info("Turning on Halogens")
        ret = self.ocs.halogens_on()
        logger.info(ret)
        # time.sleep(120)
        if 'data' in ret:
            # Start the IFU dome lamps in the background
            # N_ifu = cube_params['ifu']['dome']['N']
            t = Thread(target=self.take_dome, kwargs={'cam': self.ifu,
                                                      'N': 5,
                                                      'exptime': 180,
                                                      'readout': 2.0,
                                                      'wait': False,
                                                      'do_lamp': False
                                                      })
            t.daemon = True
            t.start()

            # Now start the RC dome lamps
            time.sleep(5)
            # N_rc = cube_params['rc']['dome']['N']
            for i in cube_params['rc']['dome']['readout']:
                logger.info(i)
                for j in cube_params['rc']['dome']['exptime']:
                    logger.info(j)
                    self.take_dome(self.rc, N=5, readout=i, do_lamp=False,
                                   wait=False, exptime=j, move=False)
            logger.info("Turning off Halogens")
            ret = self.ocs.halogens_off()
            logger.info(ret)
        else:
            send_alert_email("Halogens not turned on")

        logger.info("Starting other Lamps")
        for lamp in ['hg', 'xe']:
            if lamp in cube_params['ifu']['order']:
                N = cube_params['ifu'][lamp]['N']
                exptime = cube_params['ifu'][lamp]['exptime']
                self.take_arclamp(self.ifu, lamp, N=N, readout=1.0, move=False,
                                  exptime=exptime)

        return {'elaptime': time.time() - start,
                'data': 'Efficiency cube complete'}

    def prepare_next_observation(self, exptime=100, target_list=None,
                                 obsdatetime=None,
                                 airmass=(1, 2.5), moon_sep=(20, 180),
                                 altitude_min=15, ha=(18.75, 5.75),
                                 return_type='json',
                                 do_sort=True, do_fwhm=False,
                                 sort_columns=('priority', 'start_alt'),
                                 sort_order=(False, False), save=True,
                                 save_as=None, move=True,
                                 check_end_of_night=True, update_coords=True):
        """

        :param exptime:
        :param target_list:
        :param obsdatetime:
        :param airmass:
        :param moon_sep:
        :param altitude_min:
        :param ha:
        :param return_type:
        :param do_sort:
        :param do_fwhm:
        :param sort_columns:
        :param sort_order:
        :param save:
        :param save_as:
        :param move:
        :param check_end_of_night:
        :param update_coords:
        :return:
        """
        if not obsdatetime:
            obsdatetime = datetime.datetime.utcnow() + datetime.timedelta(
                seconds=exptime)

        if os.path.exists(self.telescope_moving_done_path):
            os.remove(self.telescope_moving_done_path)

        # Here we wait until readout starts
        while datetime.datetime.utcnow() < obsdatetime:
            time.sleep(1)

        logger.info("Getting next target")
        ret = self.sky.get_next_observable_target(
            target_list=target_list, obsdatetime=obsdatetime.isoformat(),
            airmass=airmass, moon_sep=moon_sep, altitude_min=altitude_min,
            ha=ha, do_sort=do_sort, do_fwhm=do_fwhm, return_type=return_type,
            sort_order=sort_order, sort_columns=sort_columns, save=save,
            save_as=save_as, check_end_of_night=check_end_of_night,
            update_coords=update_coords)
        logger.info("sky.get_next_observable_target status:\n%s", ret)

        if "data" in ret:
            pprint.pprint(ret['data'])

            if move:
                self.ocs.tel_move(ra=ret['data']['ra'],
                                  dec=ret['data']['dec'])

    def run_acquisition_ifumap(self, cam=None, ra=200.8974, dec=36.133,
                               equinox=2000, ra_rate=0.0, dec_rate=0.0,
                               motion_flag="", exptime=120, readout=1.0,
                               shutter='normal', move=True,
                               name='HZ44_IFU_MAPPING', obj_id=24, req_id=-50,
                               retry_on_failed_astrometry=False, tcsx=False,
                               test="", p60prid=DEF_PROG,
                               p60prnm="SEDm Ifu Mapping", p60prpi="SEDm",
                               email="",
                               retry_on_sao_on_failed_astrometry=False,
                               save_as=None, offset_to_ifu=False, epoch="",
                               non_sid_targ=False):
        """
        :return:
        :param cam:
        :param obj_id:
        :param req_id:
        :param test:
        :param p60prid:
        :param p60prnm:
        :param p60prpi:
        :param email:
        :param exptime:
        :param readout:
        :param shutter:
        :param move:
        :param name:
        :param retry_on_failed_astrometry:
        :param tcsx:
        :param ra:
        :param dec:
        :param retry_on_sao_on_failed_astrometry:
        :param save_as:
        :param equinox:
        :param ra_rate:
        :param dec_rate:
        :param motion_flag:
        :param offset_to_ifu:
        :param epoch:
        :param non_sid_targ:
        :return:
        """

        start = time.time()

        # unused parameters
        if retry_on_failed_astrometry or retry_on_sao_on_failed_astrometry:
            pass
        if tcsx or offset_to_ifu:
            pass

        # Start by moving to the target using the input rates
        if move:
            logger.info("Moving to target")
            ret = self.ocs.tel_move(name=name, ra=ra, dec=dec, equinox=equinox,
                                    ra_rate=ra_rate, dec_rate=dec_rate,
                                    motion_flag=motion_flag, epoch=epoch)
            logger.info(ret)

            if "error" in ret:
                ret = self.ocs.tel_move(name=name, ra=ra, dec=dec,
                                        equinox=equinox, ra_rate=ra_rate,
                                        dec_rate=dec_rate,
                                        motion_flag=motion_flag, epoch=epoch)
                logger.info("SECOND RETURN: %s", ret)
            # Stop sidereal tracking until after the image is completed
            if non_sid_targ:
                self.ocs.set_rates(ra=0, dec=0)

        ret = self.take_image(self.rc, shutter=shutter, readout=readout,
                              name=name, start=start, test=test,
                              save_as=save_as, imgtype='Acq_ifumap',
                              objtype='Acq_ifumap', exptime=30,
                              object_ra=ra, object_dec=dec, email=email,
                              p60prid=p60prid, p60prpi=p60prpi,
                              p60prnm=p60prnm,
                              obj_id=obj_id, req_id=req_id,
                              objfilter='r', imgset='NA',
                              is_rc=False, abpair=False)
        logger.info(ret)
        ret = self.sky.solve_offset_new(ret['data'], return_before_done=False)
        logger.info("sky.solve_offset_new status:\n%s", ret)
        if 'error' in ret:
            logger.error("Image not used: error in return")
            return {'elaptime': time.time() - start, 'error': ret['error']}
        elif 'data' in ret:
            ra = ret['data']['ra_offset']
            dec = ret['data']['dec_offset']
            ret = self.ocs.tel_offset(ra, dec)
            logger.info(ret)
            logger.info(self.ocs.tel_offset(-98.5, -111.0))
        else:
            logger.error("Image not used: no return")
            return {'elaptime': time.time() - start, 'error': 'No return'}

        offsets = [{'ra': 0, 'dec': 0}, {'ra': -5, 'dec': 0},
                   {'ra': 10, 'dec': 0}, {'ra': -5, 'dec': -5},
                   {'ra': 0, 'dec': 10}]

        for offset in offsets:
            ret = self.ocs.tel_offset(offset['ra'], offset['dec'])
            logger.info(ret)
            ret = self.take_image(cam, shutter=shutter, readout=readout,
                                  name=name, start=start, test=test,
                                  save_as=save_as, imgtype='Acq_ifumap',
                                  objtype='Acq_ifumap', exptime=exptime,
                                  object_ra=ra, object_dec=dec, email=email,
                                  p60prid=p60prid, p60prpi=p60prpi,
                                  p60prnm=p60prnm,
                                  obj_id=obj_id, req_id=req_id,
                                  objfilter='r', imgset='NA',
                                  is_rc=False, abpair=False)
            logger.info(ret)
        return {'elaptime': time.time() - start, 'data': offsets}

    def run_rc_focus_seq(self, exptime=30, foc_range=None,
                         solve=True, name="Focus", save_as=None,
                         ra=None, dec=None, equinox=2000,
                         p60prid=DEF_PROG, p60prpi='SEDm',
                         p60prnm='SEDm Calibration File',
                         get_request_id=True, req_id=-999,
                         email='jpurdum@caltech.edu', move=True):

        start = time.time()  # Start the clock on the procedure

        # get nominal rc focus based on temperature
        weather_dict = self.ocs.check_weather()
        if self.focus_guess:
            focus_temp = self.focus_temp
        else:
            if self.use_winter:
                wret = self.winter.get_weather()
                if 'data' in wret:
                    focus_temp = wret['data']['Outside_Temp']
                else:
                    focus_temp = 5.0
                    self.focus_guess = True
            else:
                focus_temp = float(
                    weather_dict['data']['inside_air_temp'])
        nominal_rc_focus = rc_focus.temp_to_focus(focus_temp) + \
            self.params['rc_focus_offset']
        img_list = []
        # error_list = []

        if ra is None or dec is None:
            ret = self.sky.get_focus_coords()
            logger.info("sky.get_focus_coords status:\n%s", ret)
            if 'data' in ret:
                ra = ret['data']['ra']
                dec = ret['data']['dec']
                if move:
                    ret = self.ocs.tel_move(name=name, ra=ra, dec=dec,
                                            equinox=equinox)
                    if 'data' not in ret:
                        logger.warning("could not move telescope,"
                                       " focusing in place.")
            else:
                logger.info("could not get focus coords, focusing in place.")
        else:
            logger.info("Using input focus coords: %s, %s", ra, dec)
            if move:
                ret = self.ocs.tel_move(name=name, ra=ra, dec=dec,
                                        equinox=equinox)
                if 'data' not in ret:
                    logger.warning("could not move telescope to input coords,"
                                   " focusing in place.")

        obj_id = self.calibration_id_dict['focus']['rc']

        if get_request_id:
            ret = self.sky.get_calib_request_id(camera='rc', N=1, exptime=0,
                                                object_id=obj_id)
            logger.info("sky.get_calib_request_id status:\n%s", ret)
            if "data" in ret:
                req_id = ret['data']

        if foc_range is None:
            # get nominal focus based on temperature
            logger.info("nominal rc focus: %.2f for temperature %.1f",
                        nominal_rc_focus, focus_temp)
            # nominal range
            if self.focus_guess:     # bigger range if we are guessing
                logger.info("Focus based on temperature estimate")
                foc_range = np.arange(nominal_rc_focus-0.5,
                                      nominal_rc_focus+0.5, 0.05)
            else:               # otherwise, smaller range
                logger.info("Focus based on temperature measure")
                foc_range = np.arange(nominal_rc_focus-0.23,
                                      nominal_rc_focus+0.23, 0.05)

        logger.info("RC Focus, focus range: %s", foc_range)
        startN = 1
        N = 1
        for pos in foc_range:

            # These request stage and lamp status at the start of the sequence
            if N == startN:
                do_stages = True
                do_lamps = True
            else:
                do_stages = False
                do_lamps = False

            N += 1
            logger.info("RC Focus - Moving to focus position: %fmm", pos)

            logger.info("TELESCOPE SECONDARY")
            if move:
                self.ocs.goto_focus(pos=pos)

            ret = self.take_image(self.rc, exptime=exptime, readout=2,
                                  start=start, save_as=save_as,
                                  imgtype='Focus', objtype='Focus',
                                  object_ra=ra, object_dec=dec,
                                  email=email, p60prid=p60prid, p60prpi=p60prpi,
                                  p60prnm=p60prnm, obj_id=obj_id,
                                  req_id=req_id, objfilter='rc_all',
                                  imgset='A', do_lamps=do_lamps,
                                  do_stages=do_stages,
                                  is_rc=True, abpair=False, name=name)
            logger.info("take_image(FOC) status:\n%s", ret)

            if 'error' in ret:
                logger.error("Skipping this image: error in return")
            elif 'data' in ret:
                img_list.append(ret['data'])
            else:
                logger.error("Skipping this image: no return")

        logger.debug("Finished RC Focus sequence")
        logger.info("focus image list:\n%s", img_list)
        # send_alert_email("Focus sequence finished")
        if solve:
            ret = self.sky.get_rc_focus(img_list,
                                        nominal_focus=nominal_rc_focus)
            logger.info("sky.get_focus status:\n%s", ret)
            if 'data' in ret:
                best_foc = round(ret['data'][0][0], 2)
                logger.info("Best FOCUS is: %s", best_foc)
            else:
                logger.warning("Could not solve!  Using Nominal focus: %s",
                               nominal_rc_focus)
                best_foc = nominal_rc_focus
        else:
            logger.warning("RC Focus not solved!  Using nominal focus")
            best_foc = nominal_rc_focus
        if best_foc:
            logger.info("TELESCOPE SECONDARY")
            self.ocs.goto_focus(pos=best_foc)
        else:
            logger.error("Unable to calculate focus")
            return {"elaptime": time.time() - start,
                    "error": "Unable to calculate focus"}

        return {"elaptime": time.time() - start,
                "data": {"focus_time": Time(datetime.datetime.utcnow()).iso,
                         "focus_temp": focus_temp,
                         "focus_pos": best_foc}}

    def run_spec_focus_seq(self, exptime=40, readout=1, foc_range=None,
                           solve=True, name="Focus", save_as=None,
                           p60prid=DEF_PROG, p60prpi='SEDm',
                           p60prnm='SEDm Calibration File',
                           get_request_id=True, req_id=-999,
                           email='jpurdum@caltech.edu',
                           do_lamp=True, lamp='dome', wait=True,
                           move=True):

        start = time.time()  # Start the clock on the procedure

        nominal_spec_focus = stages_cfg['stage1']

        obj_id = self.calibration_id_dict['focus']['ifu']
        if get_request_id:
            ret = self.sky.get_calib_request_id(camera='ifu', N=1, exptime=0,
                                                object_id=obj_id)
            logger.info("sky.get_calib_request_id status:\n%s", ret)
            if "data" in ret:
                req_id = ret['data']

        if move:
            ret = self.ocs.stow(**self.stow_profiles['calibrations'])
            if 'data' not in ret:
                logger.warning("Unable to reach cal stow, focusing in place")

        if do_lamp:
            if 'dome' in lamp:
                ret = self.ocs.halogens_on()
                logger.info("ocs.halogens_on status:\n%s", ret)
                if wait:
                    logger.info("Waiting 120 seconds for dome lamp to warm up")
                    time.sleep(120)
            else:
                ret = self.ocs.arclamp(lamp, command="ON")
                logger.info("ocs.arclamp status:\n%s", ret)

                if wait:
                    logger.info("Waiting %d seconds for %s lamp to warm up" %
                                (self.lamp_wait_time[lamp.lower()], lamp))
                    time.sleep(self.lamp_wait_time[lamp.lower()])

        if foc_range is None:
            # Range limits: 0 - 3, from 1SL? and 1SR?
            foc_range = np.arange(1.0, 1.7, .05)

        logger.info("focus type: Spec, focus range: %s", foc_range)

        img_list = []
        startN = 1
        N = 1
        for pos in foc_range:

            do_stages = True
            # These request lamp status at the start of the sequence
            if N == startN:
                do_lamps = True
            else:
                do_lamps = False

            N += 1
            logger.info("Spec-Moving to focus position: %fmm", pos)

            logger.info("IFUSTAGE 1")
            if move:
                self.ocs.move_stage(position=pos, stage_id=1)

            ret = self.take_image(self.ifu, exptime=exptime, readout=readout,
                                  start=start, save_as=save_as,
                                  imgtype='Focus', objtype='Focus',
                                  email=email, p60prid=p60prid, p60prpi=p60prpi,
                                  p60prnm=p60prnm, obj_id=obj_id,
                                  req_id=req_id, objfilter='ifu',
                                  imgset='A', do_lamps=do_lamps,
                                  do_stages=do_stages,
                                  is_rc=False, abpair=False, name=name)
            logger.info("take_image(SPEC FOC) status:\n%s", ret)

            if 'error' in ret:
                logger.error("Skipping this image: error in return")
            elif 'data' in ret:
                img_list.append(ret['data'])
            else:
                logger.error("Skipping this image: no return")

        if do_lamp:
            if 'dome' in lamp:
                ret = self.ocs.halogens_off()
                logger.info("ocs.halogens_off status:\n%s", ret)
            else:
                ret = self.ocs.arclamp(lamp, command="OFF")
                logger.info("ocs.arclamp status:\n%s", ret)

        logger.debug("Finished spec focus sequence")
        logger.info("focus image list:\n%s", img_list)
        # send_alert_email("Focus sequence finished")
        if solve:
            ret = self.sky.get_spec_focus(img_list, header_field='IFUFOCUS',
                                          nominal_focus=nominal_spec_focus,
                                          lamp=lamp)
            logger.info("sky.get_spec_focus status:\n%s", ret)
            if 'data' in ret:
                best_foc = round(ret['data'][0][0], 2)
                logger.info("Best IFU stage 1 focus is %s", best_foc)
            else:
                logger.warning("Could not solve for spec focus!  Using "
                               "nominal focus")
                best_foc = nominal_spec_focus
        else:
            logger.warning("Spec Focus not solved!  Using nominal focus")
            best_foc = nominal_spec_focus

        if best_foc:
            logger.info("IFUSTAGE 1")
            self.ocs.move_stage(position=best_foc, stage_id=1)
        else:
            logger.error("Unable to calculate focus")
            return {"elaptime": time.time() - start,
                    "error": "Unable to calculate focus"}
        return {"elaptime": time.time() - start,
                "data": {"focus_time": Time(datetime.datetime.utcnow()).iso,
                         "focus_pos": best_foc}}

    def run_ifu_focus_seq(self, exptime=240, readout=1, foc_range=None,
                          solve=False, name="Focus", save_as=None,
                          ra=0, dec=0, equinox=2000,
                          p60prid=DEF_PROG, p60prpi='SEDm',
                          p60prnm='SEDm Calibration File',
                          get_request_id=True, req_id=-999,
                          email='jpurdum@caltech.edu', move=True):

        start = time.time()  # Start the clock on the procedure

        nominal_ifu_focus = stages_cfg['stage2']

        obj_id = self.calibration_id_dict['focus']['ifu']
        if get_request_id:
            ret = self.sky.get_calib_request_id(camera='ifu', N=1, exptime=0,
                                                object_id=obj_id)
            logger.info("sky.get_calib_request_id status:\n%s", ret)
            if "data" in ret:
                req_id = ret['data']

        if ra is None or dec is None:
            stdret = self.sky.get_standard()
            logger.info("sky.get_standard status:\n%s", stdret)

            if 'data' in stdret:
                try:
                    name = stdret['data']['name']
                    ra = stdret['data']['ra']
                    dec = stdret['data']['dec']
                    exptime = stdret['data']['exptime']
                except KeyError:
                    logger.error("Invalid STD record, missing keyword")
                    return {
                        'elaptime': time.time() - start,
                        'error': 'Invalid standard record'
                    }
                if move:
                    ret = self.ocs.tel_move(name=name, ra=ra, dec=dec,
                                            equinox=equinox)
                    if 'data' not in ret:
                        logger.warning("could not move telescope,"
                                       " focusing in place.")
            else:
                logger.info("could not get focus coords, focusing in place.")
        else:
            logger.info("Using input focus coords: %s, %s", ra, dec)
            if move:
                ret = self.ocs.tel_move(name=name, ra=ra, dec=dec,
                                        equinox=equinox)
                if 'data' not in ret:
                    logger.warning("could not move telescope to input coords,"
                                   " focusing in place.")

        if foc_range is None:
            # Range limits: 0 - 8, from 2SL? and 2SR?
            foc_range = np.arange(1.6, 2.8, .1)

        logger.info("focus type: IFU focus, focus range: %s", foc_range)

        img_list = []
        startN = 1
        N = 1
        for pos in foc_range:

            do_stages = True
            # These request stage and lamp status at the start of the sequence
            if N == startN:
                do_lamps = True
            else:
                do_lamps = False

            N += 1
            logger.info("IFU-Focus-Moving to focus position: %fmm", pos)

            logger.info("IFUSTAGE 2")
            if move:
                self.ocs.move_stage(position=pos, stage_id=2)

            ret = self.take_image(self.ifu, exptime=exptime, readout=readout,
                                  start=start, save_as=save_as,
                                  imgtype='Focus', objtype='Focus',
                                  object_ra=ra, object_dec=dec,
                                  email=email, p60prid=p60prid, p60prpi=p60prpi,
                                  p60prnm=p60prnm, obj_id=obj_id,
                                  req_id=req_id, objfilter='ifu',
                                  imgset='A', do_lamps=do_lamps,
                                  do_stages=do_stages,
                                  is_rc=False, abpair=False, name=name)
            logger.info("take_image(IFU FOC) status:\n%s", ret)

            if 'error' in ret:
                logger.error("Skipping this image: error in return")
            elif 'data' in ret:
                img_list.append(ret['data'])
            else:
                logger.error("Skipping this image: no return")

        logger.debug("Finished IFU Focus sequence")
        logger.info("focus image list:\n%s", img_list)
        # send_alert_email("Focus sequence finished")
        if solve:
            ret = self.sky.get_focus(img_list, header_field='IFUFOC2',
                                     nominal_focus=nominal_ifu_focus)
            logger.info("sky.get_focus status:\n%s", ret)
            if 'data' in ret:
                best_foc = round(ret['data'][0][0], 2)
                logger.info("Best IFU stage 2 focus is %s", best_foc)
            else:
                logger.warning("Could not solve for ifu_stage2 focus, using "
                               "nominal focus")
                best_foc = nominal_ifu_focus
        else:
            logger.warning("IFU Focus not solved!  Using nominal focus")
            best_foc = nominal_ifu_focus

        if best_foc:
            logger.info("IFUSTAGE 2")
            self.ocs.move_stage(position=best_foc, stage_id=2)
        else:
            logger.error("Unable to calculate focus")
            return {"elaptime": time.time() - start,
                    "error": "Unable to calculate focus"}

        return {"elaptime": time.time() - start,
                "data": {"focus_time": Time(datetime.datetime.utcnow()).iso,
                         "focus_pos": best_foc}}

    def run_guider_seq(self, cam, guide_length=0, readout=2.0,
                       shutter='normal', guide_exptime=1, email="",
                       objfilter="", req_id=-999, obj_id=-999,
                       object_ra="", object_dec="", test="", filename='',
                       save_dir='', is_rc=True, p60prpi="", p60prid="",
                       do_corrections=True, p60prnm="", name="", save_as=None,
                       imgset=""):

        start = time.time()

        # unused parameters
        if filename is not None or save_dir is not None:
            pass

        time.sleep(5)
        # guide_exptime = 60
        end_time = datetime.datetime.utcnow() + datetime.timedelta(
            seconds=guide_length - 5)
        filename = str(abs(req_id))
        save_dir = '/home/sedm/images/%s/' % end_time.strftime('%Y%m%d')
        if readout == 2.0:
            readout_time = 7
        else:
            readout_time = 47

        self.guider_list = []
        logger.info("Guider log file parameters: %s, %s", save_dir, filename)
        if do_corrections:
            ret = self.sky.start_guider(
                start_time=None, end_time=None, exptime=guide_length,
                image_prefix="rc", max_move=None, min_move=None,
                filename=filename, save_dir=save_dir,
                data_dir=os.path.join(self.robot_image_dir,
                                      self._ut_dir_date()),
                debug=False, wait_time=5)
            logger.info("sky.start_guider status:\n%s", ret)

        guide_done = (datetime.datetime.utcnow() +
                      datetime.timedelta(seconds=guide_exptime + readout_time))

        N = 1
        while guide_done <= end_time:
            if N == 1:
                do_stages = True
                do_lamps = True
            else:
                do_stages = False
                do_lamps = False
            N += 1
            ret = ""
            try:
                ret = self.take_image(cam, exptime=guide_exptime,
                                      shutter=shutter, readout=readout,
                                      start=None, save_as=save_as, test=test,
                                      imgtype="Guider", objtype="Guider",
                                      object_ra=object_ra,
                                      object_dec=object_dec,
                                      email=email, p60prid=p60prid,
                                      p60prpi=p60prpi,
                                      p60prnm=p60prnm, obj_id=obj_id,
                                      imgset=imgset,
                                      req_id=req_id, objfilter=objfilter,
                                      do_stages=do_stages, do_lamps=do_lamps,
                                      is_rc=is_rc, abpair=False, name=name)
            except Exception as e:
                logger.error("Error taking guider image", exc_info=True)
                logger.error(str(e))

            if 'error' in ret:
                logger.error("Skipping this image: error in return")
            elif 'data' in ret:
                self.guider_list.append(ret['data'])
            else:
                send_alert_email("Error setting up guiding")
                logger.error("Skipping this image: no return")

            guide_done = (datetime.datetime.utcnow() +
                          datetime.timedelta(
                              seconds=guide_exptime + readout_time))

        logger.info("Guider Done in %s seconds", time.time() - start)

        while datetime.datetime.utcnow() < end_time:
            time.sleep(.5)

        if do_corrections:
            try:
                ret = self.sky.listen()
                logger.info("sky.listen(GUIDE) status:\n%s", ret)
            except Exception as e:
                logger.error("sky.listen ERROR: %s", str(e))
                logger.error("Error getting guider return", exc_info=True)

        logger.info("Number of guider images: %d", len(self.guider_list))
        logger.debug("Finished guider sequence for %s" % name)

    def run_standard_seq(self, cam, shutter="normal",
                         readout=1.0, name="", get_standard=True,
                         test="", save_as=None, imgtype='Standard',
                         exptime=90, ra=0, dec=0, equinox=2000,
                         epoch="", ra_rate=0, dec_rate=0, motion_flag="",
                         p60prid='2022A-calib', p60prpi='SEDm',
                         email='neill@srl.caltech.edu',
                         p60prnm='SEDm Calibration File', obj_id=-999,
                         objfilter='ifu', imgset='A', is_rc=False,
                         run_acquisition=True, req_id=-999, acq_readout=2.0,
                         offset_to_ifu=True, objtype='Standard',
                         non_sid_targ=False, guide_readout=2.0,
                         move_during_readout=True, abpair=False,
                         guide=True, guide_shutter='normal', move=True,
                         guide_exptime=10, guide_save_as=None,
                         retry_on_failed_astrometry=False, take_rc_image=False,
                         mark_status=True, status_file='', get_request_id=True):
        start = time.time()

        # unused parameters
        if move_during_readout:
            pass
        if guide_save_as is not None or status_file is not None:
            pass

        if get_standard:
            ret = self.sky.get_standard()
            logger.info("sky.get_standard status:\n%s", ret)

            if 'data' in ret:
                try:
                    name = ret['data']['name']
                    ra = ret['data']['ra']
                    dec = ret['data']['dec']
                    exptime = ret['data']['exptime']
                except KeyError:
                    logger.error("Invalid STD record, missing keyword")
                    return {'elaptime': time.time() - start,
                            'error': 'Invalid standard record'}

        if get_request_id:

            if name in self.calibration_id_dict:
                obj_id = self.calibration_id_dict[name]

            ret = self.sky.get_calib_request_id(camera=cam.prefix()['data'],
                                                N=1, exptime=exptime,
                                                object_id=obj_id)
            logger.info("sky.get_calilb_request_id status:\n%s", ret)
            if "data" in ret:
                req_id = ret['data']

        if move:
            if take_rc_image:
                ret = rc_filter_coords.offsets(ra=ra, dec=dec)
                logger.info("rc_filter_coords.offsets status:\n%s", ret)
                if 'data' in ret:
                    obs_coords = ret['data']
                else:
                    send_alert_email("No Standard Star obs_coords")
                    logger.error("ERROR")
                    obs_coords = None

                if obs_coords:
                    ret = self.ocs.tel_move(ra=obs_coords['r']['ra'],
                                            dec=obs_coords['r']['dec'],
                                            equinox=equinox,
                                            ra_rate=ra_rate,
                                            dec_rate=dec_rate,
                                            motion_flag=motion_flag,
                                            name=name,
                                            epoch=epoch)
                    if 'data' in ret:
                        pass
                    if 'bd' in name.lower():
                        rc_exptime = 5
                    elif 'hz44' in name.lower():
                        rc_exptime = 12
                    else:
                        rc_exptime = 10
                    ret = self.take_image(self.rc, exptime=rc_exptime,
                                          shutter='normal', readout=1.0,
                                          start=start, save_as=save_as,
                                          test=test,
                                          imgtype=imgtype, objtype=objtype,
                                          object_ra=ra, object_dec=dec,
                                          email=email, p60prid=p60prid,
                                          p60prpi=p60prpi,
                                          p60prnm=p60prnm, obj_id=obj_id,
                                          req_id=req_id, objfilter="r",
                                          imgset='NA', is_rc=True,
                                          abpair=abpair, name=name)
                    logger.info("take_image(STD) status:\n%s", ret)
                    if 'data' in ret:
                        pass

            if run_acquisition:
                if save_as:
                    acq_save_as = save_as.replace('ifu', 'rc')
                else:
                    acq_save_as = None
                ret = self.run_acquisition_seq(
                    self.rc, ra=ra, dec=dec, equinox=equinox, ra_rate=ra_rate,
                    dec_rate=dec_rate, motion_flag=motion_flag, exptime=30,
                    readout=acq_readout, shutter=shutter, move=move, name=name,
                    obj_id=obj_id, req_id=req_id,
                    retry_on_failed_astrometry=retry_on_failed_astrometry,
                    tcsx=False, test=test, p60prid=p60prid, p60prnm=p60prnm,
                    p60prpi=p60prpi, email=email,
                    retry_on_sao_on_failed_astrometry=False,
                    save_as=acq_save_as,
                    offset_to_ifu=offset_to_ifu, epoch=epoch,
                    non_sid_targ=non_sid_targ)
                logger.info("run_acquisition_seq(STD) status:\n%s", ret)
                if 'data' not in ret:
                    send_alert_email("No Standard Star acquisition data")
                    if mark_status:
                        # Update stuff
                        pass
                    return {'elaptime': time.time() - start, 'error': ret}

            else:
                logger.warning("Doing a blind offset to IFU!")
                ret = self.ocs.tel_move(name=name, ra=ra, dec=dec,
                                        equinox=equinox, ra_rate=ra_rate,
                                        dec_rate=dec_rate,
                                        motion_flag=motion_flag,
                                        epoch=epoch)
                logger.info("ocs.tel_move(STD) status:\n%s", ret)

                ifu_ra_offset = sedm_robot_cfg['ifu']['offset']['ra']
                ifu_dec_offset = sedm_robot_cfg['ifu']['offset']['dec']
                ret = self.ocs.tel_offset(ifu_ra_offset, ifu_dec_offset)
                logger.info("ocs.tel_offset(STD) status:\n%s", ret)

        if guide:
            logger.debug("Beginning guider sequence")
            try:
                t = Thread(target=self.run_guider_seq, kwargs={
                    'cam': self.rc,
                    'guide_length': exptime,
                    'guide_exptime': guide_exptime,
                    'readout': guide_readout,
                    'shutter': guide_shutter,
                    'name': name,
                    'email': email,
                    'objfilter': objfilter,
                    'req_id': req_id,
                    'obj_id': obj_id,
                    'test': '',
                    'is_rc': True,
                    'object_ra': ra,
                    'object_dec': dec,
                    'p60prid': p60prid,
                    'p60prpi': p60prpi,
                    'p60prnm': p60prnm
                })
                t.daemon = True
                t.start()
            except Exception as e:
                logger.exception("Error running the guider command")
                logger.error(str(e))

        count = 1
        ret = ""
        if abpair:
            exptime = math.floor(exptime / 2)
            imgset = 'A'
            count = 2

        for i in range(count):
            if abpair:
                if i == 1:
                    imgset = 'B'
            ret = self.take_image(cam, exptime=exptime,
                                  shutter=shutter, readout=readout,
                                  start=start, save_as=save_as, test=test,
                                  imgtype=imgtype, objtype=objtype,
                                  object_ra=ra, object_dec=dec,
                                  email=email, p60prid=p60prid, p60prpi=p60prpi,
                                  p60prnm=p60prnm, obj_id=obj_id,
                                  req_id=req_id, objfilter=objfilter,
                                  imgset=imgset,
                                  is_rc=is_rc, abpair=abpair, name=name)
            if 'error' in ret:
                logger.error("Bad image: error in return")
            elif 'data' in ret and mark_status:
                sky_ret = self.sky.update_target_request(req_id,
                                                         status='COMPLETED')
                logger.info("sky.update_target_request status:\n%s", sky_ret)
        if 'error' in ret:
            return {'elaptime': time.time() - start, 'error': ret['error']}
        elif 'data' in ret:
            return {'elaptime': time.time() - start, 'data': ret['data']}
        else:
            return{'elaptime': time.time() - start, 'error': 'No return'}

    def _prepare_keys(self, obsdict):
        start = time.time()
        key_dict = {}
        # time.sleep(100)

        if 'imgtype' not in obsdict:
            key_dict['imgtype'] = 'Science'
        else:
            key_dict['imgtype'] = obsdict['imgtype']

        if 'equinox' not in obsdict:
            key_dict['equinox'] = 2000
        else:
            key_dict['equinox'] = obsdict['equinox']

        if 'epoch' not in obsdict:
            key_dict['epoch'] = ""
        else:
            key_dict['epoch'] = obsdict['epoch']

        if 'ra_rate' not in obsdict:
            key_dict['ra_rate'] = 0
        else:
            key_dict['ra_rate'] = obsdict['ra_rate']

        if 'dec_rate' not in obsdict:
            key_dict['dec_rate'] = 0
        else:
            key_dict['dec_rate'] = obsdict['dec_rate']

        if 'motion_flag' not in obsdict:
            key_dict['motion_flag'] = 0
        else:
            key_dict['motion_flag'] = obsdict['motion_flag']

        if 'p60prpi' not in obsdict:
            key_dict['p60prpi'] = self.p60prpi
        else:
            key_dict['p60prpi'] = obsdict['p60prpi']

        if 'p60prid' not in obsdict:
            key_dict['p60prid'] = self.p60prid
        else:
            key_dict['p60prid'] = obsdict['p60prid']

        if 'p60prnm' not in obsdict:
            key_dict['p60prnm'] = self.p60prnm
        else:
            key_dict['p60prnm'] = obsdict['p60prnm']

        if 'req_id' not in obsdict:
            key_dict['req_id'] = self.req_id
        else:
            key_dict['req_id'] = obsdict['req_id']

        if 'obj_id' not in obsdict:
            key_dict['obj_id'] = self.req_id
        else:
            key_dict['obj_id'] = obsdict['obj_id']

        if 'non_sid_targ' not in obsdict:
            key_dict['non_sid_targ'] = False
        else:
            key_dict['non_sid_targe'] = obsdict['non_sid_targ']

        if 'guide_exptime' not in obsdict:
            key_dict['guide_exptime'] = 30
        else:
            key_dict['guide_exptime'] = obsdict['guide_exptime']

        if 'email' not in obsdict:
            key_dict['email'] = ""
        else:
            key_dict['email'] = obsdict['email']

        return {'elaptime': time.time() - start, 'data': key_dict}

    def observe_by_dict(self, obsdict, move=True, run_acquisition_ifu=True,
                        run_acquisition_rc=False, guide=True, test="",
                        mark_status=True):
        """

        :param run_acquisition_rc:
        :param guide:
        :param test:
        :param mark_status:
        :param obsdict:
        :param move:
        :param run_acquisition_ifu:
        :return:
        """
        start = time.time()
        logger.info("Starting observation by dictionary")

        if isinstance(obsdict, str):
            path = obsdict
            with open(path) as data_file:
                obsdict = json.load(data_file)
        elif not isinstance(obsdict, dict):
            return {'elaptime': time.time() - start,
                    'error': 'Input is neither json file or dictionary'}

        # Check required keywords
        if not all(key in obsdict for key in self.required_sciobs_keywords):
            return {'elaptime': time.time() - start,
                    'error': 'Missing one or more required keywords'}

        # Set any missing but non critical keywords
        ret = self._prepare_keys(obsdict)

        if 'data' not in ret:
            return {'elaptime': time.time() - start,
                    'error': 'Error prepping observing parameters'}
        kargs = ret['data']
        logger.info("Observation dictionary:\n%s", kargs)

        img_dict = {}

        pprint.pprint(obsdict)

        # Now see if target has an ifu component
        if obsdict['obs_dict']['ifu'] and self.run_ifu:
            ret = self.run_ifu_science_seq(
                self.ifu, name=obsdict['name'], test=test,
                ra=obsdict['ra'], dec=obsdict['dec'], readout=1.0,
                exptime=obsdict['obs_dict']['ifu_exptime'],
                run_acquisition=run_acquisition_ifu, objtype='Transient',
                move_during_readout=True, abpair=False, guide=guide, move=move,
                mark_status=mark_status, **kargs)

            if 'data' in ret:
                img_dict['ifu'] = {'science': ret['data'],
                                   'guider': self.guider_list}

        if obsdict['obs_dict']['rc'] and self.run_rc:
            kargs.__delitem__('guide_exptime')
            ret = self.run_rc_science_seq(
                self.rc, name=obsdict['name'], test=test,
                ra=obsdict['ra'], dec=obsdict['dec'],
                run_acquisition=run_acquisition_rc, move=move,
                objtype='Transient',
                obs_order=obsdict['obs_dict']['rc_obs_dict']['obs_order'],
                obs_exptime=obsdict['obs_dict']['rc_obs_dict']['obs_exptime'],
                obs_repeat_filter=obsdict['obs_dict']['rc_obs_dict'][
                    'obs_repeat_filter'],
                repeat=1, move_during_readout=True,
                mark_status=mark_status, **kargs)
            if 'data' in ret:
                img_dict['rc'] = ret['data']

        logger.info("Observe by dictionary complete")
        if 'error' in ret:
            return {'elaptime': time.time() - start, 'error': ret['error']}
        elif 'data' in ret:
            return {'elaptime': time.time() - start, 'data': img_dict}
        else:
            return {'elaptime': time.time() - start,
                    'error': 'Image not acquired'}

    def run_ifu_science_seq(self, cam, shutter="normal", readout=1.0, name="",
                            test="", save_as=None, imgtype='Science',
                            exptime=90, ra=0, dec=0, equinox=2000,
                            epoch="", ra_rate=0, dec_rate=0, motion_flag="",
                            p60prid='2022A-calib', p60prpi='SEDm', email='',
                            p60prnm='SEDm Calibration File', obj_id=-999,
                            objfilter='ifu', imgset='NA', is_rc=False,
                            run_acquisition=True, req_id=-999, acq_readout=2.0,
                            offset_to_ifu=True, objtype='Transient',
                            non_sid_targ=False, guide_readout=2.0,
                            move_during_readout=True, abpair=False,
                            guide=True, guide_shutter='normal', move=True,
                            guide_exptime=30,
                            retry_on_failed_astrometry=False,
                            mark_status=True, status_file=''):

        start = time.time()

        # unused parameters
        if move_during_readout:
            pass
        if status_file is not None:
            pass
        if abpair:
            pass

        if mark_status:
            sky_ret = self.sky.update_target_request(req_id, status="ACTIVE")
            logger.info("sky.update_target_request(IFU) status:\n%s", sky_ret)

        if move:
            if run_acquisition:
                if save_as:
                    acq_save_as = save_as.replace('ifu', 'rc')
                else:
                    acq_save_as = None
                ret = self.run_acquisition_seq(
                    self.rc, ra=ra, dec=dec, equinox=equinox, ra_rate=ra_rate,
                    dec_rate=dec_rate, motion_flag=motion_flag, exptime=30,
                    readout=acq_readout, shutter=shutter, move=move, name=name,
                    obj_id=obj_id, req_id=req_id,
                    retry_on_failed_astrometry=retry_on_failed_astrometry,
                    tcsx=False, test=test, p60prid=p60prid, p60prnm=p60prnm,
                    p60prpi=p60prpi, email=email,
                    retry_on_sao_on_failed_astrometry=False,
                    save_as=acq_save_as,
                    offset_to_ifu=offset_to_ifu, epoch=epoch,
                    non_sid_targ=non_sid_targ)
                logger.info("run_acquisition_seq(IFU) status:\n%s", ret)
            else:
                logger.warning("Doing a blind offset to IFU!")
                ret = self.ocs.tel_move(name=name, ra=ra, dec=dec,
                                        equinox=equinox, ra_rate=ra_rate,
                                        dec_rate=dec_rate,
                                        motion_flag=motion_flag,
                                        epoch=epoch)
                logger.info("ocs.tel_move(IFU) status:\n%s", ret)

                ifu_ra_offset = sedm_robot_cfg['ifu']['offset']['ra']
                ifu_dec_offset = sedm_robot_cfg['ifu']['offset']['dec']
                ret = self.ocs.tel_offset(ifu_ra_offset, ifu_dec_offset)
                logger.info("ocs.tel_offset(IFU) status:\n%s", ret)
            # Short exposures are more vulnerable to settling issues
            if exptime < 30.:
                time.sleep(3)   # give some time for the telescope to settle

        # Commenting this out after 2022-June primary resurfacing
        # 2023-Oct-23: Uncommenting after degradation observed
        exptime = exptime * 1.20

        if guide:
            logger.debug("Beginning sequence for guiding IFU exposure")
            try:
                t = Thread(target=self.run_guider_seq, kwargs={
                    'cam': self.rc,
                    'guide_length': exptime,
                    'guide_exptime': guide_exptime,
                    'readout': guide_readout,
                    'shutter': guide_shutter,
                    'name': name,
                    'email': email,
                    'objfilter': objfilter,
                    'req_id': req_id,
                    'obj_id': obj_id,
                    'test': '',
                    'imgset': imgset,
                    'is_rc': True,
                    'object_ra': ra,
                    'object_dec': dec,
                    'p60prid': p60prid,
                    'p60prpi': p60prpi,
                    'p60prnm': p60prnm
                })
                t.daemon = True
                t.start()
            except Exception as e:
                logger.exception("Error running the guider command")
                logger.error(str(e))

        ret = self.take_image(cam, exptime=exptime,
                              shutter=shutter, readout=readout,
                              start=start, save_as=save_as, test=test,
                              imgtype=imgtype, objtype=objtype,
                              object_ra=ra, object_dec=dec,
                              email=email, p60prid=p60prid, p60prpi=p60prpi,
                              p60prnm=p60prnm, obj_id=obj_id,
                              req_id=req_id, objfilter=objfilter,
                              imgset='A', verbose=True,
                              is_rc=is_rc, abpair=abpair, name=name)
        logger.info("take_image(IFU) status:\n%s", ret)

        if 'error' in ret:
            logger.error("Image (IFU) failed to transfer")
            sky_ret = self.sky.update_target_request(req_id, status='FAILURE')
        elif 'data' in ret and mark_status:
            sky_ret = self.sky.update_target_request(req_id, status='COMPLETED')
        else:
            sky_ret = self.sky.update_target_request(req_id, status='FAILURE')
        logger.info("sky.update_target_request(IFU) status: %s", sky_ret)

        return ret

    def run_rc_science_seq(self, cam, shutter="normal", readout=.1, name="",
                           test="", save_as=None, imgtype='Science',
                           ra=0, dec=0, equinox=2000,
                           epoch="", ra_rate=0, dec_rate=0, motion_flag="",
                           p60prid=DEF_PROG, p60prpi='SEDm', email='',
                           p60prnm='SEDm Calibration File', obj_id=-999,
                           objfilter='ifu', imgset='NA', is_rc=True,
                           run_acquisition=True, req_id=-999, acq_readout=2.0,
                           objtype='Transient', obs_order=None,
                           obs_exptime=None, obs_repeat_filter=None, repeat=1,
                           non_sid_targ=False, move_during_readout=True,
                           abpair=False, move=True,
                           retry_on_failed_astrometry=False,
                           mark_status=True, status_file=''):
        start = time.time()

        # unused parameters
        if objfilter != 'ifu' or imgset != 'NA':
            pass
        if move_during_readout:
            pass
        if status_file is not None:
            pass

        object_ra = ra
        object_dec = dec

        if mark_status:
            sky_ret = self.sky.update_target_request(req_id, status="ACTIVE")
            logger.info("sky.update_target_request status:\n%s", sky_ret)

        if move:
            if run_acquisition:
                if save_as:
                    acq_save_as = save_as.replace('ifu', 'rc')
                else:
                    acq_save_as = None
                ret = self.run_acquisition_seq(
                    self.rc, ra=ra, dec=dec, equinox=equinox, ra_rate=ra_rate,
                    dec_rate=dec_rate, motion_flag=motion_flag, exptime=30,
                    readout=acq_readout, shutter=shutter, move=move, name=name,
                    obj_id=obj_id, req_id=req_id,
                    retry_on_failed_astrometry=retry_on_failed_astrometry,
                    tcsx=True, test=test, p60prid=p60prid, p60prnm=p60prnm,
                    p60prpi=p60prpi, email=email,
                    retry_on_sao_on_failed_astrometry=False,
                    save_as=acq_save_as,
                    offset_to_ifu=False, epoch=epoch,
                    non_sid_targ=non_sid_targ)
                logger.info("run_acquisition_seq(RC) status:\n%s", ret)
                if 'data' not in ret:
                    if mark_status:
                        # Update stuff
                        pass
                    return {'elaptime': time.time() - start, 'error': ret}

            else:
                _ = self.ocs.tel_move(name=name, ra=ra, dec=dec,
                                      equinox=equinox, ra_rate=ra_rate,
                                      dec_rate=dec_rate,
                                      motion_flag=motion_flag, epoch=epoch)

        ret = rc_filter_coords.offsets(ra=ra, dec=dec)
        logger.info("rc_filter_coords.offsets() status:\n%s", ret)
        if 'data' in ret:
            obs_coords = ret['data']
        else:
            logger.error("ERROR")
            return {'elaptime': time.time() - start,
                    'error': "Unable to calculate filter coordinates"}

        img_dict = {}
        logger.info("obs_coords:\n%s", obs_coords)

        if isinstance(obs_order, str):
            obs_order = obs_order.split(',')
        if isinstance(obs_repeat_filter, str):
            obs_repeat_filter = obs_repeat_filter.split(',')
        if isinstance(obs_exptime, str):
            obs_exptime = obs_exptime.split(',')

        for i in range(repeat):
            for j in range(len(obs_order)):
                objfilter = obs_order[j]
                if move:
                    ret = self.ocs.tel_move(ra=obs_coords[objfilter]['ra'],
                                            dec=obs_coords[objfilter]['dec'],
                                            equinox=equinox,
                                            ra_rate=ra_rate,
                                            dec_rate=dec_rate,
                                            motion_flag=motion_flag,
                                            name=name,
                                            epoch=epoch)
                    logger.info("ocs.tel_move(RC) status:\n%s", ret)
                    if 'error' in ret:
                        if "-3:" in ret['error']:
                            send_alert_email("Telescope move command for "
                                             "rc science sequence failed. Check OCS Server.")
                    if 'data' not in ret:
                        continue
                for k in range(int(obs_repeat_filter[j])):
                    ret = self.take_image(cam,
                                          exptime=int(float(obs_exptime[j])),
                                          shutter=shutter, readout=readout,
                                          start=start, save_as=save_as,
                                          test=test,
                                          imgtype=imgtype, objtype=objtype,
                                          object_ra=object_ra,
                                          object_dec=object_dec,
                                          email=email, p60prid=p60prid,
                                          p60prpi=p60prpi,
                                          p60prnm=p60prnm, obj_id=obj_id,
                                          req_id=req_id, objfilter=objfilter,
                                          imgset='NA', is_rc=is_rc,
                                          abpair=abpair, name=name)
                    logger.info("take_image(RC) status:\n%s", ret)
                    if 'error' in ret:
                        logger.error("Image failed: error in return")
                    elif 'data' in ret:
                        logger.info("filter %s:\n%s", objfilter, ret)
                        if objfilter in img_dict:
                            img_dict[objfilter] += ', %s' % ret['data']
                        else:
                            img_dict[objfilter] = ret['data']
                    else:
                        logger.error("Image failed: no return")
        if mark_status:
            sky_ret = self.sky.update_target_request(req_id, status="COMPLETED")
            logger.info("sky.update_target_request status:\n%s", sky_ret)

        return {'elaptime': time.time() - start, 'data': img_dict}

    def run_acquisition_seq(self, cam, ra=None, dec=None, equinox=2000,
                            ra_rate=0.0, dec_rate=0.0, motion_flag="",
                            exptime=30, readout=2.0, shutter='normal',
                            move=True, name='Simulated', obj_id=-999,
                            req_id=-999, retry_on_failed_astrometry=False,
                            tcsx=False, test="", p60prid="", p60prnm="",
                            p60prpi="", email="",
                            retry_on_sao_on_failed_astrometry=False,
                            save_as=None, offset_to_ifu=True, epoch="",
                            non_sid_targ=False):
        """

        :return:
        :param cam:
        :param obj_id:
        :param req_id:
        :param test:
        :param p60prid:
        :param p60prnm:
        :param p60prpi:
        :param email:
        :param exptime:
        :param readout:
        :param shutter:
        :param move:
        :param name:
        :param retry_on_failed_astrometry:
        :param tcsx:
        :param ra:
        :param dec:
        :param retry_on_sao_on_failed_astrometry:
        :param save_as:
        :param equinox:
        :param ra_rate:
        :param dec_rate:
        :param motion_flag:
        :param offset_to_ifu:
        :param epoch:
        :param non_sid_targ:
        :return:
        """

        start = time.time()

        # unused parameters
        if retry_on_sao_on_failed_astrometry or retry_on_failed_astrometry:
            pass

        # Start by moving to the target using the input rates
        if move:
            ret = self.ocs.tel_move(name=name, ra=ra, dec=dec, equinox=equinox,
                                    ra_rate=ra_rate, dec_rate=dec_rate,
                                    motion_flag=motion_flag, epoch=epoch)
            logger.info("ocs.tel_move status:\n%s", ret)
            if 'error' in ret:
                if "-3:" in ret['error']:
                    send_alert_email("Telescope move command for "
                                     "ifu acquisition sequence failed. Check OCS Server.")

            logger.info("sedm.py: Pausing for 1s until telescope "
                        "is done settling")
            time.sleep(1)
            # Stop sidereal tracking until after the image is completed
            if non_sid_targ:
                self.ocs.set_rates(ra=0, dec=0)

        ret = self.take_image(cam, shutter=shutter, readout=readout,
                              name=name, start=start, test=test,
                              save_as=save_as, imgtype='Acquisition',
                              objtype='Acquisition', exptime=exptime,
                              object_ra=ra, object_dec=dec, email=email,
                              p60prid=p60prid, p60prpi=p60prpi,
                              p60prnm=p60prnm,
                              obj_id=obj_id, req_id=req_id,
                              objfilter='r', imgset='NA',
                              is_rc=True, abpair=False)
        logger.info("take_image(ACQ) status:\n%s", ret)
        if 'error' in ret:
            return {'elaptime': time.time() - start,
                    'error': 'Error acquiring acquisition image: error return'}
        elif 'data' in ret:
            # get offset to reference RC pixel
            ret = self.sky.solve_offset_new(ret['data'],
                                            return_before_done=True)
            logger.info("sky.solve_offset_new status(ACQ):\n%s", ret)
            # Move to IFU position first?
            p_ra = p_dec = None
            if move and offset_to_ifu and not tcsx:
                ifu_ra_offset = sedm_robot_cfg['ifu']['offset']['ra']
                ifu_dec_offset = sedm_robot_cfg['ifu']['offset']['dec']
                # Except if we have dec > 78 deg
                if dec > 78.0:
                    p_ra = round(ifu_ra_offset/1.0, 3)
                    p_dec = ifu_dec_offset/1.0
                    # for i in range(1):
                    #    self.ocs.tel_offset(p_ra, 0.0)
                    #    time.sleep(3)
                    #    self.ocs.tel_offset(0.0, p_dec)
                    #    time.sleep(3)               
                else:
                    # lower dec, go ahead to IFU
                    logger.info("ocs.tel_offset (to IFU) status:\n%s",
                                self.ocs.tel_offset(ifu_ra_offset,
                                                    ifu_dec_offset))
            # read offsets from sky solver
            ret = self.sky.listen()
            logger.info("sky.listen(ACQ) status:\n%s", ret)
            if 'data' in ret:
                ra_off = ret['data']['ra_offset']
                dec_off = ret['data']['dec_offset']
                # if dec > 78 deg, apply ref pix offsets in two parts (twice)?
                if dec > 78.0:
                    p_ra = p_ra + ra_off
                    p_dec = p_dec + dec_off
                    for i in range(1):
                        self.ocs.tel_offset(p_ra, 0.0)
                        time.sleep(2)
                        self.ocs.tel_offset(0.0, p_dec)
                        time.sleep(2) 
                else:
                    # otherwise apply ref pix offsets in one go
                    _ = self.ocs.tel_offset(ra_off, dec_off)
                # X the TCS if requested and if offsets are small?
                if tcsx and move and offset_to_ifu:
                    ifu_ra_offset = sedm_robot_cfg['ifu']['offset']['ra']
                    ifu_dec_offset = sedm_robot_cfg['ifu']['offset']['dec']
                    if abs(ra_off) < 100 and abs(dec_off) < 100:
                        logger.info("ocs.telx(ACQ) status:\n%s",
                                    self.ocs.telx())
                    logger.info("ocs.tel_offset to IFU status:\n%s",
                                self.ocs.tel_offset(ifu_ra_offset,
                                                    ifu_dec_offset))
                # Apply additional ra, dec offsets for non-sidereal targets
                if non_sid_targ:
                    elapsed = time.time() - start
                    ra_rate_off = round(ra_rate * (elapsed / 3600), 2)
                    dec_rate_off = round(dec_rate * (elapsed / 3600), 2)
                    ret = self.ocs.tel_offset(ra_rate_off, dec_rate_off)
                    logger.info("ocs.tel_offset(rates) status:\n%s", ret)
                    # Set the non-sideral rates
                    ret = self.ocs.set_rates(ra=ra_rate, dec=dec_rate)
                    logger.info("ocs.set_rates status:\n%s", ret)
                return {'elaptime': time.time() - start,
                        'data': 'Telescope in place with calculated offsets'}
            # no offsets can be calculated, so do thing blind
            else:
                # Apply additional ra, dec offsets for non-sidereal target
                if non_sid_targ:
                    elapsed = time.time() - start
                    ra_rate_off = round(ra_rate * (elapsed / 3600), 2)
                    dec_rate_off = round(dec_rate * (elapsed / 3600), 2)
                    ret = self.ocs.tel_offset(ra_rate_off, dec_rate_off)
                    logger.info("ocs.tel_offset(rates) status:\n%s", ret)
                    # Set the non-sideral rates
                    ret = self.ocs.set_rates(ra=ra_rate, dec=dec_rate)
                    logger.info("ocs.set_rates status:\n%s", ret)
                return {'elaptime': time.time() - start,
                        'data': 'Telescope in place with blind pointing'}

        else:
            return {'elaptime': time.time() - start,
                    'error': 'Error acquiring acquisition image: no return'}

    def run_telx_seq(self, ra=None, dec=None, equinox=2000, exptime=30,
                     test=""):
        """

        :return:
        :param test:
        :param exptime:
        :param ra:
        :param dec:
        :param equinox:
        :return:
        """

        start = time.time()

        # Start by moving to the target using the input coords
        ret = self.ocs.tel_move(name='TelXField', ra=ra, dec=dec,
                                equinox=equinox, ra_rate=0., dec_rate=0.,
                                motion_flag="", epoch="")
        logger.info("ocs.tel_move status:\n%s", ret)
        logger.info("Pausing for 1s until telescope is done settling")
        time.sleep(1)

        ret = self.take_image(self.rc, shutter='normal', readout=2.0,
                              name='TelXField', start=start, test=test,
                              save_as=None, imgtype='Acquisition',
                              objtype='Acquisition', exptime=exptime,
                              object_ra=ra, object_dec=dec,
                              email="neill@srl.caltech.edu",
                              p60prid=DEF_PROG, p60prpi="SEDm",
                              p60prnm="SEDm Calibration File",
                              obj_id=-999, req_id=-999,
                              objfilter='r', imgset='NA',
                              is_rc=True, abpair=False)
        logger.info("take_image(TELX) status:\n%s", ret)
        if 'error' in ret:
            logger.error("Bad image: error in return")
            return {'elaptime': time.time() - start,
                    'error': 'Error acquiring acquisition image: '
                             'error in return'}
        elif 'data' in ret:
            # get offset to reference RC pixel
            ret = self.sky.solve_offset_new(ret['data'],
                                            return_before_done=True)
            logger.info("sky.solve_offset_new(TELX) status:\n%s", ret)
            # read offsets from sky solver
            ret = self.sky.listen()
            logger.info("sky.listen(TELX) status:\n%s", ret)
            if 'data' in ret:
                ra_off = ret['data']['ra_offset']
                dec_off = ret['data']['dec_offset']
                logger.info("Calculated offsets: %f, %f", ra_off, dec_off)
                # Do offset, X so that no offset exceeds 100 asecs
                while abs(ra_off) > 0. or abs(dec_off) > 0.:
                    if abs(ra_off) >= 100:
                        if ra_off > 0:
                            temp_ra_off = 99.
                            ra_off -= 99.
                        else:
                            temp_ra_off = -99.
                            ra_off += 99.
                    else:
                        temp_ra_off = ra_off
                        ra_off = 0.
                    if abs(dec_off) >= 100:
                        if dec_off > 0:
                            temp_dec_off = 99.
                            dec_off -= 99.
                        else:
                            temp_dec_off = -99.
                            dec_off += 99.
                    else:
                        temp_dec_off = dec_off
                        dec_off = 0.
                    logger.info("offsetting %f, %f", temp_ra_off, temp_dec_off)
                    self.ocs.tel_offset(temp_ra_off, temp_dec_off)
                    time.sleep(1)
                    logger.info("ocs.telx(TELX) status:\n%s", self.ocs.telx())
                return {'elaptime': time.time() - start,
                        'data': 'Telescope X completed'}
            # no offsets can be calculated
            else:
                return {'elaptime': time.time() - start,
                        'error': 'No offsets calculated'}
        else:
            return {'elaptime': time.time() - start,
                    'error': 'Error acquiring acquisition image'}

    def find_nearest(self, target_file, obsdate=None):
        """
        Given a target_file (an ephemeris file in this case) use the pandas
        package to find the nearest target to the closest utdate.

        Find the nearest observation time
        :param target_file:
        :param obsdate:
        :return:
        """

        # Start the timer
        start = time.time()

        # Read in the csv file
        df = pd.read_csv(target_file)

        # Check that required keys are present
        # Note these keys must be present at the moment in the exact way
        # as shown below.  Work could be done to make the system smarter
        # but for now we just leave it up to the observer
        needed_keys = ("objname", "ra(degrees)", "dec(degrees)", "equinox",
                       "ra_rate(arcsec/hr)", "dec_rate(arcsec/hr)", "time",
                       "V")

        # Removes unintended spaces from columns
        df.columns = df.columns.str.replace(' ', '')

        exists = set(needed_keys).issubset(df.keys())

        if not exists:
            print("needed_keys: ", needed_keys)
            print("eph columns: ", df.keys())
            return {"elaptime": time.time() - start,
                    "error": 'Specified keys in csv file were not found'}

        # Convert the time frame to the datetime format
        df['time'] = pd.to_datetime(df['time'])

        # Set the index of the dataframe to be time
        df.set_index('time', inplace=True)

        # If an obsdate was not given then use the current UT time
        if not obsdate:
            obsdate = datetime.datetime.utcnow()

        # Convert the obsdate to the pandas format
        dt = pd.to_datetime(obsdate)

        # Get the number of entries
        total_ephems = len(df)
        logger.info(total_ephems)

        # Make sure the dataframe isn't empty
        if df.empty:
            return {"elaptime": time.time() - start,
                    "error": 'No data found in csv file'}

        # Find the nearest target location in the datadrame
        idx = df.index.get_loc(dt, method='nearest')

        # Ignore first and last values at the moment as
        # this likely means no entry was found close to the
        # correct time.
        # TODO: Add some logic to find the time difference
        #       from the chosen value to the given obsdate
        #       keep the value as long as it's within a specified
        #       time frame like 5-10minutes
        if idx == total_ephems - 1:
            return {"elaptime": time.time() - start,
                    "error": 'Last value picked'}
        elif idx == 0:
            return {"elaptime": time.time() - start,
                    "error": 'First value picked'}

        # Once we have the correct time we have to convert it
        # over to decimal time in order to feed it into the
        # TCS.  This will allow the TCS to correct the offsets
        # based on the current time and the time when the ephemeris
        # values were given
        logger.info("idx %d", idx)
        uttime = df.index[idx]
        decimal_time = uttime.hour + ((uttime.minute * 60) +
                                      uttime.second) / 3600.0

        # Create observing dict
        return_dict = {
            'name': df['objname'][idx],
            'RA': df['ra(degrees)'][idx],
            'Dec': df['dec(degrees)'][idx],
            'RAvel': df['ra_rate(arcsec/hr)'][idx],
            'decvel': df['dec_rate(arcsec/hr)'][idx],
            'mag': df['V'][idx],
            'epoch': decimal_time
        }

        return {"elaptime": time.time() - start, "ephemeris": return_dict}

    def get_non_sid_ephemeris_pluto(self, name, eph_time="now", eph_nsteps="1",
                                    eph_stepsize="0.00001", eph_mpc="I41",
                                    eph_faint="99", eph_type="0",
                                    eph_motion="2", eph_center="-2",
                                    eph_epoch="default", eph_resid="0"):
        """
        Use simple url to retrieve ephemeris from pluto website

        :param name:
        :param eph_time:
        :param eph_nsteps:
        :param eph_stepsize:
        :param eph_mpc:
        :param eph_faint:
        :param eph_type:
        :param eph_motion:
        :param eph_center:
        :param eph_epoch:
        :param eph_resid:
        :return:
        """

        base_url = 'https://www.projectpluto.com/cgi-bin/fo/fo_serve.cgi?'

        # Pre-process name because comet designations wreak havoc
        if name.count('_') >= 2:
            cname = name.replace('_', '/', 1)
            cname = cname.replace('_', ' ')
        elif name.count('_') == 1:
            cname = name.replace('_', ' ')
        else:
            cname = name

        safe_name = quote_plus(cname)

        url_string = base_url + 'obj_name=' + safe_name + \
            '&year=' + eph_time + '&n_steps=' + eph_nsteps + \
            '&stepsize=' + eph_stepsize + '&mpc_code=' + eph_mpc + \
            '&faint_limit=' + eph_faint + '&ephem_type=' + eph_type + \
            '&separate_motions=' + eph_motion + '&element_center=' + eph_center + \
            '&epoch=' + eph_epoch + '&resids=' + eph_resid + \
            '&language=e&file_no=3'

        logger.info(url_string)

        try:
            response = urlopen(url_string)
        except urllib.error.URLError as e:
            logger.error(str(e))
            return False

        try:
            data_json = json.loads(response.read())
        except json.decoder.JSONDecodeError as e:
            logger.error(str(e))
            return False

        if 'ephemeris' in data_json:
            eph_dict = data_json['ephemeris']['entries']['0']
            if 'RAVel' in eph_dict:
                eph_dict['RAVel'] *= 60.    # convert from asec/min to asec/hr
            if 'decvel' in eph_dict:
                eph_dict['decvel'] *= 60.   # convert from asec/min to asec/hr
            ret_dict = {'ephemeris': eph_dict}
            return ret_dict
        else:
            return False

    def get_non_sid_ephemeris_MPC(self, name, start=None, location="I41",
                                  number=1, step='1s',
                                  proper_motion='coordinate'):
        """
                Use MPC from astroquery.mpc to create an ephemeris of a
                classified object

                :param name: str - target name in obsdict
                :param start: str or Time
                :param location: str or EarthLocation - Observatory location;
                    typically an MPC observatory code
                :param number: int - number of lines to output
                :param step: str or Quantity - units of days (d), hours (h),
                    minutes (min), or seconds (s)
                :param proper_motion: str
                :return: dict
                """
        # Pre-process name because comet designations wreak havoc
        if name.count('_') >= 2:
            cname = name.replace('_', '/', 1)
            cname = cname.replace('_', ' ')
        elif name.count('_') == 1:
            cname = name.replace('_', ' ')
        else:
            cname = name

        logger.info("Using MPC package")

        try:
            # Returns a table
            eph = MPC.get_ephemeris(cname, start=start, location=location,
                                    number=number, step=step,
                                    proper_motion=proper_motion)
        except ValueError as e:
            logger.error(str(e))
            return False
        except astroquery.exceptions.InvalidQueryError as e:
            logger.error(str(e))
            return False

        if eph['Delta'][0] < 0.03:
            logger.info("Woah, this object is pretty nearby. "
                        "Better use JPL HORIZONS instead")
            try:
                # Returns a 1-row table at current time
                ephjpl = Horizons(id=cname, location=location).ephemerides()

            except ValueError as e:
                logger.error(str(e))
                return False
            except astroquery.exceptions.InvalidQueryError as e:
                logger.error(str(e))
                return False

            # Fill in nonsid_dict parameters with JPL ephemeris table values
            eph_dict = {'ISO_time': Time(ephjpl['datetime_jd'][0],
                                         format='jd').iso,
                        'RA': ephjpl['RA'][0], 'Dec': ephjpl['DEC'][0],
                        'RAvel': ephjpl['RA_rate'][0],
                        'decvel': ephjpl['DEC_rate'][0]}

        else:
            # Fill in nonsid_dict parameters with MPC ephemeris table values
            eph_dict = {'ISO_time': eph["Date"].value[0],
                        'RA': eph['RA'][0], 'Dec': eph['Dec'][0],
                        'RAvel': eph['dRA'][0], 'decvel': eph['dDec'][0]}

        ret_dict = {'ephemeris': eph_dict}
        return ret_dict

    def get_non_sid_ephemeris(self, name, eph_time="now", eph_nsteps="1",
                              eph_stepsize="0.00001", eph_mpc="I41",
                              eph_faint="99", eph_type=0, eph_motion=2,
                              eph_center=-2, eph_epoch="default", eph_resid=0,
                              eph_redact=False, eph_lang="e", eph_file=3,
                              eph_kwargs=[]):
        """
        Web driver to fetch ephemerides on an object from Project Pluto and
        MPC. Returns JSON dictionary

        Needs driver for browser (e.g. chromedriver, safaridriver, etc.)
        See https://www.selenium.dev/downloads/ for documentation

        'name' is designated solar system object name or number OR NEOCP
         un-designated name
            e.g. 2021 FG3, 6478, ZTF0Nf7

        'eph_time' is the datetime in UT. Default is now.
            Options at https://www.projectpluto.com/update8d.htm#time_entry

        'eph_nsteps' is number of steps to output. Default is 1, should not
         need more

        'eph_stepsize' is the stepsize. Default is 0.00001 days, which is
         around 1 second. Also shouldn't need changing if nsteps stays at 1

        'eph_mpc' is the Observatory Code given by the MPC. I41 is ZTF
        if 'RAVel' in eph_dict:
            eph_dict['RAVel'] *= 60.  # convert from asec/min to asec/hr
        if 'decvel' in eph_dict:
            eph_dict['decvel'] *= 60.  # convert from asec/min to asec/hr

        'eph_faint' is the limiting magnitude. Default is 99

        'eph_type' is the type of ephemeris. Default is 0 --> "Observables"

        'eph_motion' is the sky or coordinate motions of non-sidereal object.
         Default is 2 --> Separate motions in RA and dec ('/hr)

        'eph_center' is the element center (e.g. heliocentric, barycentric,
         etc.). Default is -2 --> "Automatic"

        'eph_epoch' is the reasonable epoch of current observations in JD.
         "default" is within a day of last observation in MPC database

        'eph_resid' is the residual format. Default is 0 --> '0.01"'

        'eph_redact' is default False. True if you're going to redistribute the
         pseudo-MPEC

        'eph_lang' is the ephemeris language. Default is "e" --> "English"

        'eph_file' is the output style of the ephemeris. Default is 3 -->
        "JSON ephemerides"

        'eph_kwargs' is a list of strings of ephemeris options.
         Website Default is {"sigma": ephemeris uncertainties}
         other options: {
            "alt_az":Alt/Az,
            "radial":Radial velocity,
            "phase":Phase angle,
            "pab":Phase angle bisector,
            "hel_ec":Heliocentric ecliptic,
            "ground":Ground track,
            "visib":Visibility indicator,
            "top_ec":Topocentric ecliptic,
            "unobs":Suppress unobservables,
            "comp_fr":Computer-friendly ephems,
            "lun_elong":Lunar elongation,
            "lun_alt":Lunar altitude,
            "lun_az":Lunar azimuth,
            "sky_br":Sky brightness,
            "sun_alt":Sun altitude,
            "sun_az":Sun azimuth,
            "e30":PsAng,
            "e31":PsAMV,
            "e32":PlAng,
            "e33":Galactic lat/lon,
            "e34":Galactic
            confusion
        }
        """

        # Pre-process name because comet designations wreak havoc
        if '_' in name:
            cname = name.replace('_', '/', 1)
            cname = cname.replace('_', ' ')
        else:
            cname = name

        driver = webdriver.Chrome('chromedriver')
        logger.info("Webdriver successfully installed")

        driver.get("https://www.projectpluto.com/ephem.htm")
        logger.info("Website loaded successfully")

        # Enter element names in website source code
        obj_name = driver.find_element_by_name("obj_name")
        date_time = driver.find_element_by_name("year")
        numb_steps = driver.find_element_by_name("n_steps")
        step_size = driver.find_element_by_name("stepsize")
        mpc_code = driver.find_element_by_name("mpc_code")
        faint_limit = driver.find_element_by_name("faint_limit")
        center = Select(driver.find_element_by_name("element_center"))
        epoch = driver.find_element_by_name("epoch")

        # Once page loads, clear pre-loaded entries
        obj_name.clear()
        date_time.clear()
        numb_steps.clear()
        step_size.clear()
        mpc_code.clear()
        faint_limit.clear()
        epoch.clear()

        # Reload element names after clearing fields
        obj_name = driver.find_element_by_name("obj_name")
        date_time = driver.find_element_by_name("year")
        numb_steps = driver.find_element_by_name("n_steps")
        step_size = driver.find_element_by_name("stepsize")
        mpc_code = driver.find_element_by_name("mpc_code")
        faint_limit = driver.find_element_by_name("faint_limit")
        epoch = driver.find_element_by_name("epoch")

        # Enter values in designated elements
        obj_name.send_keys(cname)
        date_time.send_keys(eph_time)
        numb_steps.send_keys(eph_nsteps)
        step_size.send_keys(eph_stepsize)
        mpc_code.send_keys(eph_mpc)
        faint_limit.send_keys(eph_faint)
        driver.find_element_by_xpath("//*[@name='ephem_type'][@value=%s]"
                                     % eph_type).click()
        if eph_type == 0:
            for option in eph_kwargs:
                driver.find_element_by_xpath(
                    "//*[@type='checkbox'][@name='%s']" % option).click()

        driver.find_element_by_xpath("//*[@name='motion'][@value=%s]"
                                     % eph_motion).click()
        center.select_by_value("%s" % eph_center)
        epoch.send_keys(eph_epoch)
        driver.find_element_by_xpath("//*[@name='resids'][@value='%s']"
                                     % eph_resid).click()
        if eph_redact:
            driver.find_element_by_name("redact_neocp").click()

        driver.find_element_by_xpath("//*[@name='language'][@value='%s']"
                                     % eph_lang).click()
        driver.find_element_by_xpath("//*[@name='file_no'][@value='%s']"
                                     % eph_file).click()

        # Submit
        driver.find_element_by_xpath(
            "//*[@type='submit'][@value=' Compute orbit and ephemerides ']").click()

        try:
            data_json = json.loads(driver.find_element_by_xpath("/html/body").text)
        except ValueError:
            driver.close()
            logger.error("No ephemeris generated")
            data_json = False

        if data_json:
            logger.info(data_json)
            driver.close()
            ret_dict = {'ephemeris': data_json['ephemeris']['entries']['0']}
        else:
            ret_dict = False

        return ret_dict

    def get_nonsideral_target(self, target_file='', target="", obsdate='',
                              target_dir=''):
        """
        Look in the specified nonsideral targets directory for text files that
        contain the ephemeris for a specfic non-sidereal target.  All files are
        expected to be in the following notation

        object_name.ut_obsdate.csv

        When an a target is specified then the program will look for that
        specific target.  Otherwise the program will look at all the ephemeris
        for the given UT date and choose the closest.

        :param target_file: str: path of a specific target file to be read in
        :param target: str: specific object name to search for, must match
            exactly with the object_name at the beginning of a file
        :param obsdate: str: ut obsdate given in YYYY-mm-dd format, if not
            supplied then the current utdate will be used
        :param target_dir: str: path for where to find the non-sidereal target
            directory
        :return:
        """

        # 1. Start the timer to keep track on how long it takes
        # to get the correct target
        start = time.time()
        ret = ""

        # 2. If a target file was given then just use that path otherwise
        # an attempt is made to find the nearest match
        if not target_file:

            # Get a usable obsdate if not given
            if not obsdate:
                obsdate_date = datetime.datetime.utcnow().strftime("%Y-%m-%d")
                obsdate = datetime.datetime.utcnow()
            else:
                # TODO: In the past a whole utc string was provided by the robot
                # this could probably be removed so that the given format was
                # always in the correct format or just switch to using the date
                # parse util function
                obsdate_date = obsdate.split()[0]

            logger.info("Checking the Nonsidereal obsdate given %s", obsdate)

            # Use the target directory given otherwise just look at the
            # defualt class path.

            # TODO: Put in a check for path
            if not target_dir:
                target_dir = self.non_sidereal_dir

            logger.info("Checking the Nonsidereal target directory %s",
                        target_dir)

            # If a target name was given then we should be able to find it's
            # ephemersis by creating the path using the notation of the
            # ephemeris files object_name.utdate.csv format
            if target:
                target_file = os.path.join(target_dir, '%s.%s.csv' %
                                           (target, obsdate_date))

            # If the above didn't work then we have to find the best target
            # by looking at all targets in the directory
            if not target_file:
                # Create the search string
                logger.info("Search string:", 'glob.glob %s*.%s.csv',
                            target_dir, obsdate_date)
                available_targets = glob.glob('%s*.%s.csv' %
                                              (target_dir, obsdate_date))
                logger.info("Nonsidereal Available targets %s",
                            available_targets)
                # If no files are found then return an error
                if len(available_targets) == 0:
                    return {'elaptime': time.time() - start,
                            'error': 'No targets available'}
                # Look for the nearest target.  The ret value should be
                # a dictionary with all relavaent info needed to create the run
                # command
                for t in available_targets:
                    # Command only returns true if an acceptable target is found
                    ret = self.find_nearest(t, obsdate=obsdate)
                    if 'data' in ret:
                        shutil.move(t, t.replace('.csv', 'txt.observed'))
                        break
        else:
            ret = self.find_nearest(target_file, obsdate=obsdate)

        if not ret:
            return {"elaptime": time.time() - start, "error": "No target found"}

        return ret

    def conditions_cleared(self):
        faults = self.ocs.check_faults()
        logger.info("ocs.check_faults returns:\n%s", faults)
        if 'data' in faults:
            if 'P200' in faults['data'] or 'WEATHER' in faults['data']:
                return False
            elif 'DOME_NOT_OPEN' in faults['data']:
                return True
            else:
                return True
        else:
            logger.info("No faults found")
            return True

    def check_dome_status(self, open_if_closed=True):
        start = time.time()
        stat = self.ocs.check_status()

        if 'data' in stat:
            ret = stat['data']['dome_shutter_status']
            if 'closed' in ret.lower():
                if open_if_closed:
                    logger.info("Opening dome")
                    open_ret = self.ocs.dome("open")
                    return {'elaptime': time.time()-start,
                            'data': open_ret}
            else:
                return {'elaptime': time.time()-start,
                        'data': "Dome already open"}

        else:
            send_alert_email("Unable to check dome status")
            return {'elaptime': time.time() - start,
                    'error': stat}

    def run_manual_command(self, manual):
        """
        Check json file for manual commands to be run.  The command is run and
        then if it is a file the location of the file is removed after
        attempting to run the command.  There is no error handling so if the
        command was not run properly it will not be attempted again to avoid
        the robot getting stuck in a loop

        :param manual: str or dict: When given a string the program assumes its
                                    the path to a json file containing the
                                    intended command to run
        """

        # start the timer
        start = time.time()
        ret = None
        ret_lab = None
        ephret = None

        # 1. Check if we have a path to json file or if a dict is already given
        if isinstance(manual, str):
            path = manual
            with open(path) as data_file:
                obsdict = json.load(data_file)

        elif not isinstance(manual, dict):
            send_alert_email("Manual: input not json file or dictionary")
            return {'elaptime': time.time() - start,
                    'error': 'Input is neither json file or dictionary'}

        else:
            obsdict = manual

        logger.info("Manual command found with the following: %s", obsdict)
        # 2. Check to see which command is being asked to run.  If the command
        # key is not given then the file is removed and we exit the function
        if 'command' in obsdict:
            command = obsdict['command']
        else:
            send_alert_email("MANUAL: No command in file")

            return {'elaptime': time.time() - start,
                    'error': 'Command not found in manual dict'}

        logger.info("Executing manual command: %s", command)
        # 3, Run the given command.  Right now the program can do
        # standards, focus, and rc and ifu non sidereal targets.
        # other commands can be added as needed
        if command.lower() == "standard":
            ret = self.run_standard_seq(self.ifu)
            ret_lab = "MANUAL: run_standard_seq status:"
        elif command.lower() == "telx":
            ret = self.run_telx_seq(ra=obsdict['ra'], dec=obsdict['dec'])
            ret_lab = "MANUAL: run_telx_seq status:"
        elif command.lower() == "focus":
            if 'range_start' in obsdict and 'range_stop' in obsdict and \
                    'range_increment' in obsdict:
                ret = self.run_rc_focus_seq(foc_range=np.arange(
                                             obsdict['range_start'],
                                             obsdict['range_stop'],
                                             obsdict['range_increment']))
                ret_lab = "MANUAL: run_focus_seq status:"
            else:
                ret = self.run_rc_focus_seq()
                ret_lab = "MANUAL(def): run_focus_seq status:"

        elif command.lower() == "ifu":

            if 'target' in obsdict:
                if 'ra' in obsdict and 'dec' in obsdict:
                    RA = obsdict['ra']
                    DEC = obsdict['dec']
                else:
                    coords = SkyCoord.from_name(obsdict['target'], parse=True)
                    RA = coords.ra.degree     # converted to ra hours elsewhere
                    DEC = coords.dec.degree
                    logger.info("Target Coords: %s",
                                coords.to_string("hmsdms", sep=":"))
                logger.info("decimal deg: %f, %f", RA, DEC)

                if 'allocation_id' in obsdict:
                    alloc_id = obsdict['allocation_id']
                else:
                    alloc_id = None
                ret = self.sky.get_manual_request_id(name=obsdict['target'],
                                                     typedesig="f",
                                                     exptime=obsdict['exptime'],
                                                     allocation_id=alloc_id,
                                                     ra=RA, dec=DEC)
                logger.info("sky.get_manual_request_id status:\n%s", ret)
                if 'data' in ret:
                    req_id = ret['data']['request_id']
                    obj_id = ret['data']['object_id']
                    p60prid = ret['data']['p60prid']
                    p60prnm = ret['data']['p60prnm']
                    p60prpi = ret['data']['p60prpi']
                else:
                    req_id = -999
                    obj_id = -999
                    p60prid = '2022A-calib'
                    p60prnm = 'SEDm calibration'
                    p60prpi = 'SEDm'
                    logger.warning("Unable to obtain request data")
            else:
                logger.error("target not found")
                return {'elaptime': time.time() - start,
                        'error': "ifu 'target' in manual dict not found"}

            ret = self.run_ifu_science_seq(
                self.ifu, name=obsdict['target'], imgtype='Science',
                exptime=obsdict['exptime'], ra=RA, dec=DEC, readout=1.0,
                p60prid=p60prid, p60prpi=p60prpi, email='',
                p60prnm=p60prnm, req_id=req_id,
                obj_id=obj_id, objfilter='ifu',
                run_acquisition=True, objtype='Transient', non_sid_targ=False,
                guide_readout=2.0, move_during_readout=True, abpair=False,
                guide_shutter='normal', move=True, guide_exptime=30,
                retry_on_failed_astrometry=False,
                mark_status=True, status_file='')

            ret_lab = "MANUAL: run_ifu_science_seq status:"

        elif command.lower() == "rc":

            if 'target' in obsdict:
                if 'ra' in obsdict and 'dec' in obsdict:
                    RA = obsdict['ra']
                    DEC = obsdict['dec']
                else:
                    coords = SkyCoord.from_name(obsdict['target'], parse=True)
                    RA = coords.ra.degree      # .to_string('hour', sep=":")
                    DEC = coords.dec.degree    # .to_string('deg', sep=":")
                    logger.info("Target Coords: %s",
                                coords.to_string("hmsdms", sep=":"))
                logger.info("decimal deg: %f, %f", RA, DEC)

            else:
                logger.error("target not found")
                return {'elaptime': time.time() - start,
                        'error': "rc 'target' in manual dict not found"}

            if 'allocation_id' in obsdict:
                alloc_id = obsdict['allocation_id']
            else:
                alloc_id = None
            ret = self.sky.get_manual_request_id(name=obsdict['target'],
                                                 typedesig="f",
                                                 exptime=obsdict['exptime'],
                                                 allocation_id=alloc_id,
                                                 ra=RA, dec=DEC)
            logger.info("sky.get_manual_request_id status:\n%s", ret)
            if 'data' in ret:
                req_id = ret['data']['request_id']
                obj_id = ret['data']['object_id']
                p60prid = ret['data']['p60prid']
                p60prnm = ret['data']['p60prnm']
                p60prpi = ret['data']['p60prpi']
            else:
                req_id = -999
                obj_id = -999
                p60prid = '2022A-calib'
                p60prnm = 'SEDm calibration'
                p60prpi = 'SEDm'
                logger.warning("Unable to obtain request data")

            if 'repeat_filter' in obsdict:
                repeat_filter = obsdict['repeat_filter']
            else:
                nfilt = len(obsdict['rcfilter'].split(','))
                if nfilt == 1:
                    repeat_filter = '1'
                else:
                    repeat_filter = '1,' * (nfilt - 1) + '1'

            if 'n_sets' in obsdict:
                n_sets = int(obsdict['n_sets'])
            else:
                n_sets = 1

            ret = self.run_rc_science_seq(
                self.rc, shutter="normal", readout=.1, name=obsdict['target'],
                test="", save_as=None, imgtype='Science', ra=RA, dec=DEC,
                equinox=2000, p60prid=p60prid, p60prpi=p60prpi,
                email='', p60prnm=p60prnm, obj_id=obj_id,
                objfilter='RC%s' % (obsdict['rcfilter']), imgset='NA',
                is_rc=True, run_acquisition=True, req_id=req_id,
                acq_readout=2.0, objtype='Transient',
                obs_order=obsdict['rcfilter'], obs_exptime=obsdict['exptime'],
                obs_repeat_filter=repeat_filter, repeat=n_sets,
                non_sid_targ=False, move_during_readout=True, abpair=False,
                move=True, retry_on_failed_astrometry=False, mark_status=True,
                status_file='')

            ret_lab = "MANUAL: run_rc_science_seq status:"

        elif command.lower() == "nonsid_ifu":

            if 'target' in obsdict:

                if "ephem_file" in obsdict:
                    if "obsdate" in obsdict:
                        obsdate = obsdict['obsdate']
                    else:
                        obsdate = datetime.datetime.utcnow()
                    logger.info('Using ephemeris file %s at date: %s',
                                obsdict['ephem_file'], obsdate)
                    ephret = self.find_nearest(obsdict['ephem_file'],
                                               obsdate=obsdate)
                else:
                    if "obsdate" in obsdict:
                        obsdate = obsdict['obsdate']
                        obsdateMPC = obsdict['obsdate']
                        logger.info('Using ephemeris website at date: %s',
                                    obsdate)
                    else:
                        obsdate = "now"
                        obsdateMPC = None
                        now = Time.now()
                        logger.info('Using ephemeris website at date: %s', now)
                    if "candidate" in obsdict:
                        try:
                            ephret = self.get_non_sid_ephemeris_pluto(
                                name=obsdict['target'], eph_time=obsdate)
                        except ValueError:
                            logger.warning("ValueError exception")
                            pass
                    else:
                        try:
                            ephret = self.get_non_sid_ephemeris_MPC(
                                name=obsdict['target'], start=obsdateMPC)
                        except ValueError:
                            logger.warning("ValueError exception")
                            pass
                logger.info("Returned ephemeris:\n%s", ephret)

            else:
                send_alert_email("MANUAL: cannot find 'target' in JSON file")
                return {'elaptime': time.time() - start,
                        'error': "nonsid_ifu 'target' in manual dict not found"}

            if 'allocation_id' in obsdict:
                alloc_id = obsdict['allocation_id']
            else:
                alloc_id = None

            if 'ephemeris' not in ephret:

                return {"elaptime": time.time() - start,
                        "error: 'ephemeris' not in return": ephret}

            nonsid_dict = ephret['ephemeris']
            if 'epoch' not in nonsid_dict:
                if 'ISO_time' in nonsid_dict:
                    nonsid_dict['epoch'] = iso_to_epoch(nonsid_dict['ISO_time'])
                else:
                    epdate = datetime.datetime.utcnow()
                    nonsid_dict[
                        'epoch'] = epdate.hour + (epdate.minute * 60
                                                  + epdate.second) / 3600.0
                    logger.warning('ISO_time not found, '
                                   'using default value (now) for epoch')
            logger.info("Using epoch: %f", nonsid_dict['epoch'])

            ret = self.sky.get_manual_request_id(name=obsdict['target'],
                                                 allocation_id=alloc_id,
                                                 exptime=obsdict['exptime'],
                                                 ra=nonsid_dict['RA'],
                                                 dec=nonsid_dict['Dec'],
                                                 typedesig="e")
            logger.info("sky.get_manual_request_id status:\n%s", ret)
            if 'data' in ret:
                req_id = ret['data']['request_id']
                obj_id = ret['data']['object_id']
                p60prid = ret['data']['p60prid']
                p60prnm = ret['data']['p60prnm']
                p60prpi = ret['data']['p60prpi']
            else:
                req_id = -999
                obj_id = -999
                p60prid = '2022B-Asteroids'
                p60prnm = 'Near-Earth Asteroid'
                p60prpi = 'SEDm'
                logger.warning("Unable to obtain request data")

            # offset target position from ephemeris coordinates
            if 'RA_offset' in obsdict:
                offset = obsdict['RA_offset']
                # convert to decimal degrees if not already
                if ':' in str(offset):
                    offset = SkyCoord(ra=offset, dec="0:0:0", unit=(u.hourangle, u.deg)).ra.deg
                nonsid_dict['RA'] += offset
            if 'Dec_offset' in obsdict:
                offset = obsdict['Dec_offset']
                # convert to decimal degrees if not already
                if ':' in str(offset):
                    offset = SkyCoord(ra='0:0:0', dec=offset, unit=(u.hourangle, u.deg)).dec.deg
                nonsid_dict['Dec'] += offset

            ret = self.run_ifu_science_seq(
                self.ifu, name=obsdict['target'], imgtype='Science',
                exptime=obsdict['exptime'],
                ra=nonsid_dict['RA'], dec=nonsid_dict['Dec'],
                equinox=2000, epoch=nonsid_dict['epoch'],
                ra_rate=nonsid_dict['RAvel'],
                dec_rate=nonsid_dict['decvel'], motion_flag="1",
                p60prid=p60prid, p60prpi=p60prpi, email='',
                p60prnm=p60prnm, req_id=req_id,
                obj_id=obj_id, objfilter='ifu',
                run_acquisition=True, objtype='Transient', non_sid_targ=True,
                guide_readout=2.0, move_during_readout=True, abpair=False,
                guide=False, guide_shutter='normal', move=True,
                guide_exptime=30, retry_on_failed_astrometry=False,
                mark_status=True, status_file='')

            ret_lab = "MANUAL(nonsid): run_ifu_science_seq status:"

        elif command.lower() == "nonsid_rc":

            if 'target' in obsdict:

                if "ephem_file" in obsdict:
                    if "obsdate" in obsdict:
                        obsdate = obsdict['obsdate']
                    else:
                        obsdate = datetime.datetime.utcnow()
                    logger.info('Using ephemeris file %s at date: %s',
                                obsdict['ephem_file'], obsdate)
                    ephret = self.find_nearest(obsdict['ephem_file'],
                                               obsdate=obsdate)
                else:
                    if "obsdate" in obsdict:
                        obsdate = obsdict['obsdate']
                        obsdateMPC = obsdict['obsdate']
                        logger.info('Using ephemeris website at date: %s',
                                    obsdate)
                    else:
                        obsdate = "now"
                        obsdateMPC = None
                        now = Time.now()
                        logger.info('Using ephemeris website at date: %s',
                                    now)
                    if "candidate" in obsdict:
                        try:
                            ephret = self.get_non_sid_ephemeris_pluto(
                                name=obsdict['target'], eph_time=obsdate)
                        except ValueError:
                            logger.warning("ValueError exception")
                            pass
                    else:
                        try:
                            ephret = self.get_non_sid_ephemeris_MPC(
                                name=obsdict['target'], start=obsdateMPC)
                        except ValueError:
                            logger.warning("ValueError exception")
                            pass

                logger.info("get_non_sid_ephemeris return:\n%s", ephret)

            else:
                send_alert_email("Manual: cannot find 'target' in JSON file")
                return {'elaptime': time.time() - start,
                        'error': "nonsid_rc 'target' in manual dict not found"}

            if 'allocation_id' in obsdict:
                alloc_id = obsdict['allocation_id']
            else:
                alloc_id = None

            if 'ephemeris' not in ephret:

                return {"elaptime": time.time() - start,
                        "error: 'ephemeris' not in return": ephret}

            nonsid_dict = ephret['ephemeris']
            if 'epoch' not in nonsid_dict:
                if 'ISO_time' in nonsid_dict:
                    nonsid_dict['epoch'] = iso_to_epoch(nonsid_dict['ISO_time'])
                else:
                    epdate = datetime.datetime.utcnow()
                    nonsid_dict[
                        'epoch'] = epdate.hour + (epdate.minute * 60
                                                  + epdate.second) / 3600.0
                    logger.warning('ISO_time not found, '
                                   'using default value (now) for epoch')
            logger.info("Using epoch: %f", nonsid_dict['epoch'])

            ret = self.sky.get_manual_request_id(name=obsdict['target'],
                                                 allocation_id=alloc_id,
                                                 exptime=obsdict['exptime'],
                                                 ra=nonsid_dict['RA'],
                                                 dec=nonsid_dict['Dec'],
                                                 typedesig="e")
            logger.info("sky.get_manual_request_id status:\n%s", ret)
            if 'data' in ret:
                req_id = ret['data']['request_id']
                obj_id = ret['data']['object_id']
                p60prid = ret['data']['p60prid']
                p60prnm = ret['data']['p60prnm']
                p60prpi = ret['data']['p60prpi']
            else:
                req_id = -999
                obj_id = -999
                p60prid = '2022B-Asteroids'
                p60prnm = 'Near-Earth Asteroid'
                p60prpi = 'SEDm'
                logger.warning("Unable to obtain request data")

            if 'repeat_filter' in obsdict:
                repeat_filter = obsdict['repeat_filter']
            else:
                nfilt = len(obsdict['rcfilter'].split(','))
                if nfilt == 1:
                    repeat_filter = '1'
                else:
                    repeat_filter = '1,' * (nfilt - 1) + '1'

            if 'n_sets' in obsdict:
                n_sets = int(obsdict['n_sets'])
            else:
                n_sets = 1

            # offset target position from ephemeris coordinates
            if 'RA_offset' in obsdict:
                offset = obsdict['RA_offset']
                # convert to decimal degrees if not already
                if ':' in str(offset):
                    offset = SkyCoord(ra=offset, dec="0:0:0", unit=(u.hourangle, u.deg)).ra.deg
                nonsid_dict['RA'] += offset
            if 'Dec_offset' in obsdict:
                offset = obsdict['Dec_offset']
                # convert to decimal degrees if not already
                if ':' in str(offset):
                    offset = SkyCoord(ra='0:0:0', dec=offset, unit=(u.hourangle, u.deg)).dec.deg
                nonsid_dict['Dec'] += offset

            ret = self.run_rc_science_seq(
                self.rc, shutter="normal", readout=.1, name=obsdict['target'],
                test="", save_as=None, imgtype='Science',
                ra=nonsid_dict['RA'], dec=nonsid_dict['Dec'], equinox=2000,
                epoch=nonsid_dict['epoch'], ra_rate=nonsid_dict['RAvel'],
                dec_rate=nonsid_dict['decvel'], motion_flag="1",
                p60prid=p60prid, p60prpi=p60prpi, email='', p60prnm=p60prnm,
                obj_id=obj_id, objfilter='RC%s' % (obsdict['rcfilter']),
                imgset='NA', is_rc=True, run_acquisition=True, req_id=req_id,
                acq_readout=2.0, objtype='Transient',
                obs_order=obsdict['rcfilter'],
                obs_exptime=obsdict['exptime'],
                obs_repeat_filter=repeat_filter, repeat=n_sets,
                non_sid_targ=True, move_during_readout=True, abpair=False,
                move=True, retry_on_failed_astrometry=False, mark_status=True,
                status_file='')

            ret_lab = "MANUAL(nonsid): run_rc_science_seq status:\n"

        return {"elaptime": time.time() - start, "label": ret_lab,
                "success": ret}

    def gzip_images(self, ob_dir):
        """Gzip the night's images"""
        if not ob_dir:
            self.obs_dir = os.path.join(self.robot_image_dir,
                                        self._ut_dir_date())
            ob_dir = self.obs_dir
            if not os.path.exists(ob_dir):
                send_alert_email("Cannot gzip images; ob_dir does not exist")
                logger.error("Error: ob_dir %s does not exist!", ob_dir)
                return

        flist = glob.glob(os.path.join(ob_dir, "*.fits"))
        for fl in flist:
            subprocess.run(["gzip", fl])
        logger.info("%d images gzipped in %s", len(flist), ob_dir)


if __name__ == "__main__":
    x = SEDm()
    x.initialize()
    # x.take_datacube_eff()
    print("Doing test")
    # ret = x.run_standard_seq(x.ifu, move=False)
    # ret = x.ocs.stow(**x.stow_profiles['calibrations'])
    # ret = x.run_manual_command("/home/sedm/SEDMv5/common_files/manual.json")
    # print(ret)

    x.take_bias(x.ifu, N=1, test=' test')
    # x.take_bias(x.rc, N=1)

    # x.take_twilight(x.ifu, move=False, max_time=10)
    # x.take_twilight(x.rc, move=False, max_time=10)
