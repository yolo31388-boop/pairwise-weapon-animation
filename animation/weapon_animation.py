"""
武器动画系统 - 核心模块（修复版）

修复内容：
- 检视动画实际播放、播放期间不可打断、结束后有冷却
- 换弹动画被打断后武器恢复到稳定状态，动画时长与实际换弹时间一致，按武器类型区分
- 开火动画带后坐力（武器上跳）与枪口闪光，受射速限制，连续射击后坐力叠加，按武器类型区分
- 武器状态机：状态互斥、状态间有过渡（收枪动作）、按优先级仲裁
- 动画与逻辑同步：换弹动画播完才填弹、支持网络延迟补偿、姿态插值平滑不跳变
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
import math
import time


@dataclass
class Config:
    """配置项"""
    pass


# 武器类型参数表：换弹时长 / 射速间隔 / 后坐力 / 弹匣容量
WEAPON_STATS = {
    "pistol": {"reload_time": 1.2, "fire_interval": 0.25, "recoil": 2.5, "magazine": 12},
    "rifle":  {"reload_time": 2.0, "fire_interval": 0.10, "recoil": 1.2, "magazine": 30},
    "sniper": {"reload_time": 3.0, "fire_interval": 1.50, "recoil": 6.0, "magazine": 5},
}

# 状态优先级：数值越大优先级越高，低优先级不能打断高优先级
STATE_PRIORITY = {
    "idle": 0,
    "transition": 1,
    "inspect": 2,
    "fire": 3,
    "reload": 4,
}

# 各状态的武器举枪姿态目标值（1.0=完全举起，越低越收枪）
POSE_TARGETS = {
    "idle": 1.0,
    "transition": 0.2,
    "inspect": 0.6,
    "fire": 1.0,
    "reload": 0.4,
}

INSPECT_DURATION = 3.0       # 检视动画时长（秒）
INSPECT_COOLDOWN = 5.0       # 检视冷却（秒）
TRANSITION_DURATION = 0.25   # 收枪/举枪过渡时长（秒）
MUZZLE_FLASH_TIME = 0.05     # 枪口闪光持续（秒）
RECOIL_RECOVERY_SPEED = 6.0  # 后坐力恢复速度（每秒）
MAX_RECOIL = 12.0            # 后坐力叠加上限
POSE_LERP_SPEED = 12.0       # 姿态插值速度


class WeaponAnimation:
    """武器动画系统核心类"""

    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}
        self.weapon_type = self.config.get("weapon_type", "rifle")
        if self.weapon_type not in WEAPON_STATS:
            raise ValueError("未知武器类型: %s" % self.weapon_type)
        self._stats = WEAPON_STATS[self.weapon_type]
        self._clock = 0.0
        self._history = []
        self._state = "idle"
        self._state_time = 0.0
        self._state_duration = 0.0
        self._pending_state = None
        self._current_animation = None
        self._ammo = self._stats["magazine"]
        self._recoil = 0.0
        self._muzzle_flash = 0.0
        self._last_shot_clock = -math.inf
        self._inspect_cooldown_remaining = 0.0
        self._pose = {"raise": 1.0}
        self._target_pose = {"raise": 1.0}

    # ------------------------------------------------------------------
    # 只读属性
    # ------------------------------------------------------------------
    @property
    def state(self) -> str:
        return self._state

    @property
    def ammo(self) -> int:
        return self._ammo

    @property
    def recoil(self) -> float:
        return self._recoil

    @property
    def state_duration(self) -> float:
        return self._state_duration

    @property
    def current_animation(self) -> Optional[Dict]:
        return self._current_animation

    @property
    def muzzle_flash_active(self) -> bool:
        return self._muzzle_flash > 0.0

    @property
    def inspect_cooldown_remaining(self) -> float:
        return self._inspect_cooldown_remaining

    @property
    def animation_progress(self) -> float:
        if self._state_duration <= 0.0:
            return 0.0
        return min(1.0, self._state_time / self._state_duration)

    @property
    def history(self) -> List[Dict]:
        return list(self._history)

    # ------------------------------------------------------------------
    # 帧更新
    # ------------------------------------------------------------------
    def update(self, dt: float):
        """每帧更新：推进状态机、后坐力恢复、姿态插值"""
        if dt <= 0:
            return
        self._clock += dt
        if self._inspect_cooldown_remaining > 0.0:
            self._inspect_cooldown_remaining = max(0.0, self._inspect_cooldown_remaining - dt)
        if self._recoil > 0.0:
            self._recoil = max(0.0, self._recoil - RECOIL_RECOVERY_SPEED * dt)
        if self._muzzle_flash > 0.0:
            self._muzzle_flash = max(0.0, self._muzzle_flash - dt)
        # 状态机推进，剩余时间向后续状态结转
        if self._state != "idle":
            self._state_time += dt
            if self._current_animation is not None:
                self._current_animation["elapsed"] = self._state_time
            while self._state != "idle" and self._state_time >= self._state_duration:
                leftover = self._state_time - self._state_duration
                self._finish_state()
                self._state_time = leftover
        # 姿态插值：平滑逼近目标姿态，避免跳变
        factor = min(1.0, POSE_LERP_SPEED * dt)
        for key, target in self._target_pose.items():
            current = self._pose.get(key, target)
            self._pose[key] = current + (target - current) * factor

    def reset(self):
        """重置状态"""
        self.__init__(self.config)

    def set_weapon(self, weapon_type: str):
        """切换武器类型，回到空闲状态"""
        if weapon_type not in WEAPON_STATS:
            raise ValueError("未知武器类型: %s" % weapon_type)
        self.weapon_type = weapon_type
        self._stats = WEAPON_STATS[weapon_type]
        self._ammo = self._stats["magazine"]
        self._recoil = 0.0
        self._muzzle_flash = 0.0
        self._enter_idle()

    # ------------------------------------------------------------------
    # 检视动画：实际播放、不可打断、有冷却
    # ------------------------------------------------------------------
    def play_inspect(self) -> bool:
        """请求播放检视动画。成功返回 True，被占用或冷却中返回 False。"""
        if self._state != "idle":
            return False  # 互斥：其他动作进行中不能检视，检视中也不可打断
        if self._inspect_cooldown_remaining > 0.0:
            return False  # 冷却中，防止反复刷检视
        self._current_animation = {
            "name": "inspect_" + self.weapon_type,
            "duration": INSPECT_DURATION,
            "elapsed": 0.0,
        }
        self._enter_state("inspect", INSPECT_DURATION)
        return True

    # ------------------------------------------------------------------
    # 换弹动画：打断后恢复、时长匹配、按武器类型
    # ------------------------------------------------------------------
    def reload_animation(self) -> bool:
        """请求播放换弹动画。动画时长与武器实际换弹时间一致。"""
        if not self._can_request("reload"):
            return False
        duration = self._stats["reload_time"]
        self._current_animation = {
            "name": "reload_" + self.weapon_type,
            "duration": duration,
            "elapsed": 0.0,
        }
        self._enter_state("reload", duration)
        return True

    def interrupt(self) -> bool:
        """打断当前可打断的动作并恢复到稳定状态（检视不可打断）。"""
        if self._state == "reload":
            # 换弹被打断：弹药不填，武器经收枪过渡恢复，不会卡在中间状态
            self._current_animation = None
            self._history.append({"event": "reload_interrupted", "time": self._clock})
            self._enter_state("transition", TRANSITION_DURATION, pending="idle")
            return True
        if self._state == "inspect":
            return False  # 检视动画不可打断
        if self._state == "fire":
            self._current_animation = None
            self._enter_idle()
            return True
        return False

    def interrupt_reload(self) -> bool:
        """打断换弹（interrupt 的语义化封装）。"""
        return self._state == "reload" and self.interrupt()

    # ------------------------------------------------------------------
    # 开火动画：后坐力 + 枪口闪光，受射速限制，按武器类型
    # ------------------------------------------------------------------
    def fire_animation(self) -> bool:
        """请求开火动画。成功返回 True。"""
        if self._state not in ("idle", "fire"):
            return False  # 互斥：换弹/检视/过渡中不能开火
        if self._ammo <= 0:
            return False  # 弹匣已空
        if self._clock - self._last_shot_clock < self._stats["fire_interval"]:
            return False  # 射速限制
        self._last_shot_clock = self._clock
        self._ammo -= 1
        # 后坐力：武器上跳，连续射击叠加（有上限），随后平滑恢复
        self._recoil = min(MAX_RECOIL, self._recoil + self._stats["recoil"])
        self._muzzle_flash = MUZZLE_FLASH_TIME
        self._current_animation = {
            "name": "fire_" + self.weapon_type,
            "duration": self._stats["fire_interval"],
            "elapsed": 0.0,
        }
        self._enter_state("fire", self._stats["fire_interval"])
        return True

    # ------------------------------------------------------------------
    # 武器状态机：互斥 + 过渡 + 优先级
    # ------------------------------------------------------------------
    def weapon_state_machine(self) -> Dict:
        """返回状态机当前快照。"""
        return {
            "state": self._state,
            "state_time": self._state_time,
            "state_duration": self._state_duration,
            "pending_state": self._pending_state,
            "priority": STATE_PRIORITY[self._state],
        }

    def can_transition(self, action: str) -> bool:
        """查询当前是否允许进入某个动作状态。"""
        if action == "fire" and self._state == "fire":
            return True
        return self._can_request(action)

    def _can_request(self, action: str) -> bool:
        """状态机仲裁：互斥 + 优先级。低优先级不能打断高优先级。"""
        if self._state == "idle":
            return True
        if self._state == "inspect":
            return False  # 检视动画不可打断
        if self._state == "transition":
            return False  # 收枪/举枪过渡中不可操作
        return STATE_PRIORITY[action] > STATE_PRIORITY[self._state]

    def _enter_state(self, state: str, duration: float, pending: Optional[str] = None):
        self._state = state
        self._state_time = 0.0
        self._state_duration = duration
        self._pending_state = pending
        self._target_pose["raise"] = POSE_TARGETS[state]
        self._history.append({"event": "enter_" + state, "time": self._clock})

    def _enter_idle(self):
        self._state = "idle"
        self._state_time = 0.0
        self._state_duration = 0.0
        self._pending_state = None
        self._current_animation = None
        self._target_pose["raise"] = POSE_TARGETS["idle"]

    def _finish_state(self):
        finished = self._state
        self._history.append({"event": finished + "_finished", "time": self._clock})
        if finished == "inspect":
            self._inspect_cooldown_remaining = INSPECT_COOLDOWN
            self._current_animation = None
            # 检视结束经收枪过渡回到空闲，不跳变
            self._enter_state("transition", TRANSITION_DURATION, pending="idle")
        elif finished == "reload":
            # 动画播完的瞬间才完成换弹逻辑，保证动画与逻辑同步
            self._ammo = self._stats["magazine"]
            self._current_animation = None
            # 换弹结束必须经收枪过渡，不能直接变开火
            self._enter_state("transition", TRANSITION_DURATION, pending="idle")
        elif finished == "fire":
            self._current_animation = None
            self._enter_idle()
        else:  # transition 及其他
            self._enter_idle()

    # ------------------------------------------------------------------
    # 动画同步：逻辑一致 + 延迟补偿 + 插值
    # ------------------------------------------------------------------
    def get_pose(self) -> Dict:
        """当前渲染姿态（已插值，平滑无跳变）。"""
        return {
            "raise": self._pose["raise"],
            "recoil": self._recoil,
            "muzzle_flash": self._muzzle_flash > 0.0,
        }

    def animation_sync(self) -> Dict:
        """生成网络同步快照：动画状态与逻辑状态一致。"""
        return {
            "timestamp": self._clock,
            "weapon_type": self.weapon_type,
            "state": self._state,
            "state_time": self._state_time,
            "state_duration": self._state_duration,
            "ammo": self._ammo,
            "recoil": self._recoil,
            "pose": self.get_pose(),
        }

    def apply_sync(self, snapshot: Dict, latency: float = 0.0):
        """应用远端快照，latency 为网络延迟（秒）。

        延迟补偿：快照在传输期间流逝的时间会立即结算（如换弹完成填弹），
        保证其他玩家看到的动画与实际操作状态一致；
        姿态通过插值平滑过渡到目标，不会跳变。
        """
        weapon_type = snapshot.get("weapon_type", self.weapon_type)
        if weapon_type != self.weapon_type:
            self.set_weapon(weapon_type)
        self._state = snapshot["state"]
        self._state_time = snapshot["state_time"] + max(0.0, latency)
        self._state_duration = snapshot["state_duration"]
        self._ammo = snapshot["ammo"]
        self._recoil = snapshot["recoil"]
        if self._state != "idle":
            self._current_animation = {
                "name": self._state + "_" + self.weapon_type,
                "duration": self._state_duration,
                "elapsed": self._state_time,
            }
            self._target_pose["raise"] = POSE_TARGETS.get(self._state, 1.0)
        else:
            self._current_animation = None
            self._target_pose["raise"] = POSE_TARGETS["idle"]
        # 延迟补偿：结算传输期间已完成的状态
        while self._state != "idle" and self._state_time >= self._state_duration:
            leftover = self._state_time - self._state_duration
            self._finish_state()
            self._state_time = leftover
