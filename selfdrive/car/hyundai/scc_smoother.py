import copy
import random
import numpy as np
from common.numpy_fast import clip, interp, mean
from cereal import car
from selfdrive.config import Conversions as CV
from selfdrive.car.hyundai.values import Buttons
from common.params import Params
from selfdrive.controls.lib.drive_helpers import V_CRUISE_MAX, V_CRUISE_MIN, V_CRUISE_DELTA_KM, V_CRUISE_DELTA_MI
from selfdrive.controls.lib.lane_planner import TRAJECTORY_SIZE
from selfdrive.road_speed_limiter import road_speed_limiter_get_max_speed

# do not modify
MIN_SET_SPEED = V_CRUISE_MIN
MAX_SET_SPEED = V_CRUISE_MAX

LIMIT_ACCEL = 10.
LIMIT_DECEL = 18.

ALIVE_COUNT = [6, 8]
WAIT_COUNT = [12, 13, 14, 15, 16]
AliveIndex = 0
WaitIndex = 0

MIN_CURVE_SPEED = 32.

EventName = car.CarEvent.EventName

ButtonType = car.CarState.ButtonEvent.Type
ButtonPrev = ButtonType.unknown
ButtonCnt = 0
LongPressed = False

class CruiseState:
  STOCK = 0
  SMOOTH = 1
  COUNT = 2

class SccSmoother:

  @staticmethod
  def get_alive_count():
    global AliveIndex
    count = ALIVE_COUNT[AliveIndex]
    AliveIndex += 1
    if AliveIndex >= len(ALIVE_COUNT):
      AliveIndex = 0
    return count

  @staticmethod
  def get_wait_count():
    global WaitIndex
    count = WAIT_COUNT[WaitIndex]
    WaitIndex += 1
    if WaitIndex >= len(WAIT_COUNT):
      WaitIndex = 0
    return count

  def __init__(self, accel_gain, decel_gain, curvature_gain):

    self.state = int(Params().get('SccSmootherState'))
    self.scc_smoother_enabled = Params().get('SccSmootherEnabled') == b'1'
    self.slow_on_curves = Params().get('SccSmootherSlowOnCurves') == b'1'

    self.sync_set_speed_while_gas_pressed = Params().get('SccSmootherSyncGasPressed') == b'1'
    self.switch_only_with_gap = Params().get('SccSmootherSwitchGapOnly') == b'1'

    self.longcontrol = Params().get('LongControlEnabled') == b'1'

    self.accel_gain = accel_gain
    self.decel_gain = decel_gain
    self.curvature_gain = curvature_gain

    self.last_cruise_buttons = Buttons.NONE
    self.target_speed = 0.

    self.started_frame = 0
    self.wait_timer = 0
    self.alive_timer = 0
    self.btn = Buttons.NONE

    self.alive_count = ALIVE_COUNT
    random.shuffle(WAIT_COUNT)

    self.state_changed_alert = False

    self.slowing_down = False
    self.slowing_down_alert = False
    self.slowing_down_sound_alert = False

    self.max_speed = 0.
    self.curve_speed = 0.

    self.fused_decel = []

  def reset(self):

    self.wait_timer = 0
    self.alive_timer = 0
    self.btn = Buttons.NONE
    self.target_speed = 0.

    self.max_speed = 0.
    self.curve_speed = 0.

    self.fused_decel.clear()

    self.slowing_down = False
    self.slowing_down_alert = False
    self.slowing_down_sound_alert = False

  @staticmethod
  def create_clu11(packer, frame, bus, clu11, button):
    values = copy.copy(clu11)
    values["CF_Clu_CruiseSwState"] = button
    values["CF_Clu_AliveCnt1"] = frame
    return packer.make_can_msg("CLU11", bus, values)

  def is_active(self, frame):
    return frame - self.started_frame <= max(ALIVE_COUNT) + max(WAIT_COUNT)

  def dispatch_buttons(self, CC, CS):
    changed = False
    if self.last_cruise_buttons != CS.cruise_buttons:
      self.last_cruise_buttons = CS.cruise_buttons

      if not CS.cruiseState_enabled:
        if (not self.switch_only_with_gap and CS.cruise_buttons == Buttons.CANCEL) or CS.cruise_buttons == Buttons.GAP_DIST:
          self.state += 1
          if self.state >= CruiseState.COUNT:
            self.state = 0

          Params().put('SccSmootherState', str(self.state))
          self.state_changed_alert = True
          changed = True

    CC.sccSmoother.state = self.state
    return changed

  def inject_events(self, events):
    if self.state_changed_alert:
      self.state_changed_alert = False
      events.add(EventName.sccSmootherStatus)

    if self.slowing_down_sound_alert:
      self.slowing_down_sound_alert = False
      events.add(EventName.slowingDownSpeedSound)
    elif self.slowing_down_alert:
      events.add(EventName.slowingDownSpeed)

  def cal_max_speed(self, frame, CC, CS, sm, clu11_speed, controls):

    limit_speed, road_limit_speed, left_dist, first_started, max_speed_log = road_speed_limiter_get_max_speed(CS, controls.v_cruise_kph)

    self.cal_curve_speed(sm, clu11_speed * CV.KPH_TO_MS, frame)
    if self.slow_on_curves and self.curve_speed >= MIN_CURVE_SPEED:
      max_speed = min(controls.v_cruise_kph, self.curve_speed)
    else:
      max_speed = controls.v_cruise_kph

    max_speed_log = "{:.1f}/{:.1f}".format(float(limit_speed), float(clu11_speed))

    lead_speed = self.get_long_lead_speed(CS, clu11_speed, sm)
    if lead_speed >= MIN_SET_SPEED:
      max_speed = min(max_speed, lead_speed)

    if limit_speed >= 30:

      if first_started:
        self.max_speed = clu11_speed

      max_speed = min(max_speed, limit_speed)

      if clu11_speed > limit_speed:

        if not self.slowing_down_alert and not self.slowing_down:
          self.slowing_down_sound_alert = True
          self.slowing_down = True

        self.slowing_down_alert = True

      else:
        self.slowing_down_alert = False

    else:
      self.slowing_down_alert = False
      self.slowing_down = False

    self.update_max_speed(int(max_speed + 0.5))

    return road_limit_speed, left_dist, max_speed_log

  def update(self, enabled, can_sends, packer, CC, CS, frame, apply_accel, controls):

    clu11_speed = CS.clu11["CF_Clu_Vanz"]
    road_limit_speed, left_dist, max_speed_log = self.cal_max_speed(frame, CC, CS, controls.sm, clu11_speed, controls)
    CC.sccSmoother.roadLimitSpeed = road_limit_speed
    CC.sccSmoother.roadLimitSpeedLeftDist = left_dist

    controls.cruiseVirtualMaxSpeed = float(clip(CS.cruiseState_speed * CV.MS_TO_KPH, MIN_SET_SPEED, self.max_speed))

    CC.sccSmoother.longControl = self.longcontrol
    CC.sccSmoother.cruiseVirtualMaxSpeed = controls.cruiseVirtualMaxSpeed
    CC.sccSmoother.cruiseRealMaxSpeed = controls.v_cruise_kph

    ascc_enabled = CS.acc_mode and enabled and CS.cruiseState_enabled \
                   and 1 < CS.cruiseState_speed < 255 and not CS.brake_pressed

    if not self.longcontrol:
      if not self.scc_smoother_enabled:
        self.reset()
        return

      if self.dispatch_buttons(CC, CS):
        self.reset()
        return

      if self.state == CruiseState.STOCK or not ascc_enabled or CS.standstill or CS.cruise_buttons != Buttons.NONE:

        #CC.sccSmoother.logMessage = '{:.2f},{:d},{:d},{:d},{:d},{:.1f},{:d},{:d},{:d}' \
        #  .format(float(apply_accel*CV.MS_TO_KPH), int(CS.acc_mode), int(enabled), int(CS.cruiseState_enabled), int(CS.standstill), float(CS.cruiseState_speed),
        #          int(CS.cruise_buttons), int(CS.brake_pressed), int(CS.gas_pressed))

        CC.sccSmoother.logMessage = max_speed_log
        self.reset()
        self.wait_timer = max(ALIVE_COUNT) + max(WAIT_COUNT)
        return

      accel, override_acc = self.cal_acc(apply_accel, CS, clu11_speed, controls.sm)

    else:
      accel = 0.
      CC.sccSmoother.state = self.state = CruiseState.STOCK

      if not ascc_enabled:
        self.reset()

    self.cal_target_speed(accel, CC, CS, clu11_speed, controls)

    #CC.sccSmoother.logMessage = '{:.1f}/{:.1f}, {:d}/{:d}/{:d}, {:d}' \
    #  .format(float(override_acc), float(accel), int(self.target_speed), int(self.curve_speed), int(road_limit_speed), int(self.btn))

    CC.sccSmoother.logMessage = max_speed_log

    if self.wait_timer > 0:
      self.wait_timer -= 1
    elif ascc_enabled:

      if self.alive_timer == 0:
        self.btn = self.get_button(clu11_speed, CS.cruiseState_speed * CV.MS_TO_KPH)
        self.alive_count = SccSmoother.get_alive_count()

      if self.btn != Buttons.NONE:

        can_sends.append(SccSmoother.create_clu11(packer, self.alive_timer, CS.scc_bus, CS.clu11, self.btn))

        if self.alive_timer == 0:
          self.started_frame = frame

        self.alive_timer += 1

        if self.alive_timer >= self.alive_count:
          self.alive_timer = 0
          self.wait_timer = SccSmoother.get_wait_count()
          self.btn = Buttons.NONE
      else:
        if self.longcontrol and self.target_speed >= MIN_SET_SPEED:
          self.target_speed = 0.
    else:
      if self.longcontrol:
        self.target_speed = 0.


  def get_button(self, clu11_speed, current_set_speed):

    if self.target_speed < MIN_SET_SPEED:
      return Buttons.NONE

    error = self.target_speed - current_set_speed
    if abs(error) < 0.9:
      return Buttons.NONE

    return Buttons.RES_ACCEL if error > 0 else Buttons.SET_DECEL

  def get_lead(self, sm):

    radar = sm['radarState']
    if radar.leadOne.status:
      return radar.leadOne

    return None

  def cal_acc(self, apply_accel, CS, clu11_speed, sm):

    cruise_gap = clip(CS.cruise_gap, 1., 4.)

    override_acc = 0.
    #v_ego = clu11_speed * CV.KPH_TO_MS
    op_accel = apply_accel * CV.MS_TO_KPH

    lead = self.get_lead(sm)
    if lead is None:
      accel = op_accel
    else:

      d = lead.dRel - 5.

      # Tuned by stonerains

      if 0. < d < -lead.vRel * (7.7 + cruise_gap) * 2. and lead.vRel < -1.:
        t = d / lead.vRel
        acc = -(lead.vRel / t) * CV.MS_TO_KPH * 1.84
        override_acc = acc
        accel = (op_accel + acc) / 2.
      else:
        if 40 > lead.dRel > 12 and clu11_speed < 15.0 * CV.MS_TO_KPH:
          accel = op_accel * 3.8
        else:
          accel = op_accel * interp(clu11_speed, [0., 30., 38., 50., 51., 60., 100.],
                                    [2.3, 3.4, 3.2, 1.7, 1.65, 1.4, 1.0])

    if accel > 0.:
      accel *= self.accel_gain * interp(clu11_speed, [35., 60., 100.], [1.5, 1.25, 1.2])
    else:
      accel *= self.decel_gain * 1.8

    return clip(accel, -LIMIT_DECEL, LIMIT_ACCEL), override_acc

  def get_long_lead_speed(self, CS, clu11_speed, sm):

    if self.longcontrol and self.scc_smoother_enabled:
      lead = self.get_lead(sm)
      if lead is not None:
        d = lead.dRel - 5.
        cruise_gap = clip(CS.cruise_gap, 1., 4.)
        if 0. < d < -lead.vRel * (9. + cruise_gap) * 2. and lead.vRel < -1.:
          t = d / lead.vRel
          accel = -(lead.vRel / t) * CV.MS_TO_KPH
          accel *= self.decel_gain * 1.6

          if accel < 0.:
            target_speed = clu11_speed + accel
            target_speed = max(target_speed, MIN_SET_SPEED)
            return target_speed

    return 0

  def cal_curve_speed(self, sm, v_ego, frame):

    if frame % 10 == 0:
      md = sm['modelV2']
      if len(md.position.x) == TRAJECTORY_SIZE and len(md.position.y) == TRAJECTORY_SIZE:
        x = md.position.x
        y = md.position.y
        dy = np.gradient(y, x)
        d2y = np.gradient(dy, x)
        curv = d2y / (1 + dy ** 2) ** 1.5
        curv = curv[5:TRAJECTORY_SIZE - 10]
        a_y_max = 2.975 - v_ego * 0.0375  # ~1.85 @ 75mph, ~2.6 @ 25mph
        v_curvature = np.sqrt(a_y_max / np.clip(np.abs(curv), 1e-4, None))
        model_speed = np.mean(v_curvature) * 0.9 * self.curvature_gain
               
        if model_speed < v_ego:
          self.curve_speed = float(max(model_speed * CV.MS_TO_KPH, MIN_CURVE_SPEED))
        else:
          self.curve_speed = 300.

        if np.isnan(self.curve_speed):
          self.curve_speed = 300.
      else:
        self.curve_speed = 300.

  def cal_target_speed(self, accel, CC, CS, clu11_speed, controls):

    if not self.longcontrol:
      if CS.gas_pressed:
        self.target_speed = clu11_speed
        if clu11_speed > controls.v_cruise_kph and self.sync_set_speed_while_gas_pressed:
          set_speed = clip(clu11_speed, MIN_SET_SPEED, MAX_SET_SPEED)
          controls.v_cruise_kph = set_speed
      else:
        self.target_speed = clu11_speed + accel

      self.target_speed = clip(self.target_speed, MIN_SET_SPEED, self.max_speed)

    else:
      if CS.gas_pressed and CS.cruiseState_enabled:
        if clu11_speed + 2. > controls.v_cruise_kph and self.sync_set_speed_while_gas_pressed:
          set_speed = clip(clu11_speed + 2., MIN_SET_SPEED, MAX_SET_SPEED)
          self.target_speed = set_speed

  def update_max_speed(self, max_speed):

    if not self.longcontrol or self.max_speed <= 0:
      self.max_speed = max_speed
    else:
      kp = 0.01
      error = max_speed - self.max_speed
      self.max_speed = self.max_speed + error * kp

  def get_fused_accel(self, apply_accel, stock_accel, sm):

    dRel = 0.
    lead = self.get_lead(sm)
    if lead is not None:
      dRel = lead.dRel

      if stock_accel < apply_accel < -0.1:
        stock_weight = interp(dRel, [2., 25.], [1., 0.])
        apply_accel = apply_accel * (1. - stock_weight) + stock_accel * stock_weight

    self.fused_decel.append(apply_accel)
    if len(self.fused_decel) > 3:
      self.fused_decel.pop(0)

    return mean(self.fused_decel), dRel

  @staticmethod
  def update_cruise_buttons(controls, CS, longcontrol): # called by controlds's state_transition

    car_set_speed = CS.cruiseState.speed * CV.MS_TO_KPH
    is_cruise_enabled = car_set_speed != 0 and car_set_speed != 255 and CS.cruiseState.enabled and controls.CP.enableCruise

    if is_cruise_enabled:
      if longcontrol or controls.CC.sccSmoother.state == CruiseState.STOCK:
        v_cruise_kph = CS.cruiseState.speed * CV.MS_TO_KPH
      else:
        v_cruise_kph = SccSmoother.update_v_cruise(controls.v_cruise_kph, CS.buttonEvents, controls.enabled, controls.is_metric)
    else:
      v_cruise_kph = 0

    if controls.is_cruise_enabled != is_cruise_enabled:
      controls.is_cruise_enabled = is_cruise_enabled

      if controls.is_cruise_enabled:
        v_cruise_kph = CS.cruiseState.speed * CV.MS_TO_KPH
      else:
        v_cruise_kph = 0

      controls.LoC.reset(v_pid=CS.vEgo)

    controls.v_cruise_kph = v_cruise_kph


  @staticmethod
  def update_v_cruise(v_cruise_kph, buttonEvents, enabled, metric):

    global ButtonCnt, LongPressed, ButtonPrev
    if enabled:
      if ButtonCnt:
        ButtonCnt += 1
      for b in buttonEvents:
        if b.pressed and not ButtonCnt and (b.type == ButtonType.accelCruise or b.type == ButtonType.decelCruise):
          ButtonCnt = 1
          ButtonPrev = b.type
        elif not b.pressed and ButtonCnt:
          if not LongPressed and b.type == ButtonType.accelCruise:
            v_cruise_kph += 1 if metric else 1 * CV.MPH_TO_KPH
          elif not LongPressed and b.type == ButtonType.decelCruise:
            v_cruise_kph -= 1 if metric else 1 * CV.MPH_TO_KPH
          LongPressed = False
          ButtonCnt = 0
      if ButtonCnt > 70:
        LongPressed = True
        V_CRUISE_DELTA = V_CRUISE_DELTA_KM if metric else V_CRUISE_DELTA_MI
        if ButtonPrev == ButtonType.accelCruise:
          v_cruise_kph += V_CRUISE_DELTA - v_cruise_kph % V_CRUISE_DELTA
        elif ButtonPrev == ButtonType.decelCruise:
          v_cruise_kph -= V_CRUISE_DELTA - -v_cruise_kph % V_CRUISE_DELTA
        ButtonCnt %= 70
      v_cruise_kph = clip(v_cruise_kph, MIN_SET_SPEED, MAX_SET_SPEED)

    return v_cruise_kph


