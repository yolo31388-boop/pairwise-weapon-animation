"""
武器动画系统 - 核心模块（修复版）

修复内容：
- 检视动画实际播放（武器不消失）、播放期间不可打断、结束后有冷却
- 换弹动画被打断后武器姿态恢复、动画时长与实际换弹时间严格匹配、按武器类型区分
- 开火动画带后坐力（武器上跳）、受射速限制不叠加、按武器类型区分
- 武器状态机：状态互斥、切换经过收枪过渡、按优先级仲裁
- 动画与逻辑同步：换弹完成与弹药填充同一帧发生、同步包考虑网络延迟、姿态插值平滑
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
import math
import time


@dataclass
class Config:
    """配置项"""
    pass


# 各武器类型的动画/逻辑参数：换弹时长、射速间隔、后坐力均按类型区分
WEAPON_STATS = {
    "pistol":  {"reload_time": 1.2, "fire_interval": 0.15, "recoil_pitch": 2.0, "recoil_kick": 0.02, "magazine": 12},
    "rifle":   {"reload_time": 2.2, "fire_interval": 0.10, "recoil_pitch": 1.2, "recoil_kick": 0.03, "magazine": 30},
    "sniper":  {"reload_time": 3.2, "fire_interval": 1.20, "recoil_pitch": 6.0, "recoil_kick": 0.10, "magazine": 5},
    "shotgun": {"reload_time": 2.8, "fire_interval": 0.80, "recoil_pitch": 4.0, "recoil_kick": 0.08, "magazine": 8},
}
DEFAULT_WEAPON_TYPE = "rifle"

# 状态机状态
STATE_IDLE = "idle"
STATE_TRANSITION = "transition"    # 收枪/过渡动作
STATE_RELOADING = "reloading"
STATE_INSPECTING = "inspecting"
STATE_FIRING = "firing"

# 状态优先级：数值越大优先级越高，低优先级请求不能打断高优先级状态
STATE_PRIORITY = {
    STATE_IDLE: 0,
    STATE_TRANSITION: 1,
    STATE_INSPECTING: 2,
    STATE_RELOADING: 3,
    STATE_FIRING: 4,
}


@dataclass
class Pose:
    """武器姿态（位置 + 欧拉角）"""
    pitch: float = 0.0
    yaw: float = 0.0
    roll: float = 0.0
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0

    def copy(self) -> "Pose":
        return Pose(self.pitch, self.yaw, self.roll, self.x, self.y, self.z)

    def lerp(self, other: "Pose", t: float) -> "Pose":
        """线性插值，t 自动钳制到 [0, 1]"""
        t = max(0.0, min(1.0, t))
        return Pose(
            pitch=self.pitch + (other.pitch - self.pitch) * t,
            yaw=self.yaw + (other.yaw - self.yaw) * t,
            roll=self.roll + (other.roll - self.roll) * t,
            x=self.x + (other.x - self.x) * t,
            y=self.y + (other.y - self.y) * t,
            z=self.z + (other.z - self.z) * t,
        )


class WeaponAnimation:
    """武器动画系统核心类"""

    INSPECT_DURATION = 3.0       # 检视动画时长（秒）
    INSPECT_COOLDOWN = 5.0       # 检视冷却（秒）
    TRANSITION_DURATION = 0.25   # 收枪过渡时长（秒）
    MUZZLE_FLASH_TIME = 0.05     # 枪口闪光持续（秒）
    RECOIL_RECOVERY_SPEED = 6.0  # 后坐力恢复速度（每秒衰减比例）
    POSE_BLEND_SPEED = 12.0      # 姿态插值速度
    MAX_RECOIL_STACK = 3.0       # 后坐力叠加上限（单发后坐力的倍数）

    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}
        self._state = {}
        self._history = []

        self.weapon_type = self.config.get("weapon_type", DEFAULT_WEAPON_TYPE)
        if self.weapon_type not in WEAPON_STATS:
            raise ValueError("未知武器类型: %r" % self.weapon_type)
        self.latency = float(self.config.get("latency", 0.0))

        self.time = 0.0
        self.idle_pose = Pose()
        self.pose = Pose()
        self.target_pose = Pose()

        # 后坐力偏移（叠加在目标姿态上，随时间恢复）
        self.recoil_pitch = 0.0
        self.recoil_kick = 0.0

        self.muzzle_flash = False
        self._muzzle_flash_timer = 0.0

        stats = WEAPON_STATS[self.weapon_type]
        self.magazine_size = stats["magazine"]
        self.ammo = self.magazine_size

        self._inspect_cooldown_remaining = 0.0
        self._last_fire_time = None
        self._weapon_visible = True

        self._enter_state(STATE_IDLE)

    # ------------------------------------------------------------------
    # 状态机
    # ------------------------------------------------------------------
    @property
    def current_state(self) -> str:
        return self._state.get("name", STATE_IDLE)

    @property
    def state_time(self) -> float:
        return self._state.get("time", 0.0)

    def _enter_state(self, name: str, **data):
        prev = self._state.get("name")
        self._state = {"name": name, "time": 0.0}
        self._state.update(data)
        self._history.append((prev, name))

    def _can_request(self, state: str) -> bool:
        """互斥 + 优先级仲裁：同一时刻只允许一个动作状态"""
        cur = self.current_state
        if cur == STATE_IDLE:
            return True
        if cur == STATE_TRANSITION:
            return False  # 过渡动作播放完之前不接受新请求
        if cur == STATE_INSPECTING:
            return False  # 检视动画不可打断
        if cur == state:
            return False
        return STATE_PRIORITY[state] > STATE_PRIORITY[cur]

    def weapon_state_machine(self) -> Dict:
        """返回状态机快照：当前状态、优先级、各动作是否可请求、转换历史"""
        return {
            "state": self.current_state,
            "state_time": self.state_time,
            "priority": STATE_PRIORITY[self.current_state],
            "can_fire": self.current_state in (STATE_IDLE, STATE_RELOADING),
            "can_reload": self._can_request(STATE_RELOADING),
            "can_inspect": self._can_request(STATE_INSPECTING)
                           and self._inspect_cooldown_remaining <= 0.0,
            "history": list(self._history),
        }

    # ------------------------------------------------------------------
    # 检视动画
    # ------------------------------------------------------------------
    def play_inspect(self) -> bool:
        """播放检视动画。播放期间不可打断，结束后进入冷却。返回是否受理。"""
        if self._inspect_cooldown_remaining > 0.0:
            return False
        if not self._can_request(STATE_INSPECTING):
            return False
        self._enter_state(STATE_INSPECTING,
                          duration=self.INSPECT_DURATION,
                          interruptible=False)
        # 实际播放动画：武器抬起并翻转展示，全程可见
        self._weapon_visible = True
        self.target_pose = Pose(pitch=15.0, yaw=30.0, roll=10.0, x=0.05, y=-0.02)
        return True

    def inspect_progress(self) -> float:
        if self.current_state != STATE_INSPECTING:
            return 0.0
        return min(1.0, self.state_time / self._state["duration"])

    def is_weapon_visible(self) -> bool:
        return self._weapon_visible

    @property
    def inspect_cooldown_remaining(self) -> float:
        return self._inspect_cooldown_remaining

    # ------------------------------------------------------------------
    # 换弹动画
    # ------------------------------------------------------------------
    def reload_animation(self, weapon_type: Optional[str] = None) -> bool:
        """请求换弹。先播放收枪过渡，再进入换弹；动画时长与实际换弹时间一致。"""
        if weapon_type is not None:
            self.set_weapon_type(weapon_type)
        if not self._can_request(STATE_RELOADING):
            return False
        # 状态转换经过收枪过渡，不是瞬间切换
        self._enter_state(STATE_TRANSITION,
                          duration=self.TRANSITION_DURATION,
                          next_state=STATE_RELOADING)
        self.target_pose = Pose(pitch=-20.0, y=-0.10)
        return True

    def _start_reloading(self):
        stats = WEAPON_STATS[self.weapon_type]
        self._enter_state(STATE_RELOADING,
                          duration=stats["reload_time"],
                          ammo_before=self.ammo)
        self.target_pose = Pose(pitch=-35.0, y=-0.15, roll=-10.0)

    def reload_duration(self) -> float:
        """当前武器类型的换弹时长（动画时长与逻辑时长相同）"""
        return WEAPON_STATS[self.weapon_type]["reload_time"]

    def reload_progress(self) -> float:
        if self.current_state != STATE_RELOADING:
            return 0.0
        return min(1.0, self.state_time / self._state["duration"])

    def interrupt_reload(self) -> bool:
        """打断换弹：武器姿态立即恢复待机，弹药保持打断前的值，不卡在半空。"""
        if self.current_state != STATE_RELOADING:
            return False
        self.ammo = self._state.get("ammo_before", self.ammo)
        self._enter_state(STATE_IDLE)
        self.target_pose = self.idle_pose.copy()
        self.pose = self.idle_pose.copy()
        self.recoil_pitch = 0.0
        self.recoil_kick = 0.0
        return True

    # ------------------------------------------------------------------
    # 开火动画
    # ------------------------------------------------------------------
    def fire_animation(self, weapon_type: Optional[str] = None) -> bool:
        """开火：枪口闪光 + 后坐力上跳。受射速限制，后坐力按武器类型且有叠加上限。"""
        if weapon_type is not None:
            self.set_weapon_type(weapon_type)
        cur = self.current_state
        if cur == STATE_RELOADING:
            # 开火优先级高于换弹：打断换弹并恢复武器姿态
            self.interrupt_reload()
        elif cur != STATE_IDLE:
            return False  # 检视/过渡中不能开火（状态互斥）

        stats = WEAPON_STATS[self.weapon_type]
        # 射速限制：间隔不足的射击被丢弃，动画不叠加
        if (self._last_fire_time is not None
                and self.time - self._last_fire_time < stats["fire_interval"]):
            return False
        if self.ammo <= 0:
            return False

        self._last_fire_time = self.time
        self.ammo -= 1
        self.muzzle_flash = True
        self._muzzle_flash_timer = self.MUZZLE_FLASH_TIME
        # 后坐力：武器上跳 + 后坐，按武器类型区分，叠加有上限
        self.recoil_pitch = min(self.recoil_pitch + stats["recoil_pitch"],
                                stats["recoil_pitch"] * self.MAX_RECOIL_STACK)
        self.recoil_kick = min(self.recoil_kick + stats["recoil_kick"],
                               stats["recoil_kick"] * self.MAX_RECOIL_STACK)
        self._history.append((None, STATE_FIRING))
        return True

    # ------------------------------------------------------------------
    # 动画同步
    # ------------------------------------------------------------------
    def animation_sync(self, remote_state: Optional[Dict] = None,
                       latency: Optional[float] = None) -> Dict:
        """生成同步包（按网络延迟做前向预测）；传入远端状态时通过插值平滑对齐。"""
        lat = self.latency if latency is None else float(latency)
        progress = 0.0
        duration = self._state.get("duration", 0.0)
        if duration > 0:
            # 延迟补偿：把动画进度向前预测 lat 秒，远端看到的与实际状态一致
            progress = min(1.0, (self.state_time + lat) / duration)
        packet = {
            "state": self.current_state,
            "progress": progress,
            "pose": self.pose.copy(),
            "ammo": self.ammo,
            "timestamp": self.time + lat,
        }
        if remote_state is not None:
            # 不直接跳变到远端姿态，而是设为目标姿态，由 update 插值逼近
            remote_pose = remote_state.get("pose")
            if isinstance(remote_pose, Pose):
                self.target_pose = remote_pose.copy()
        return packet

    @staticmethod
    def interpolate(a: Pose, b: Pose, t: float) -> Pose:
        """姿态插值，供同步/过渡使用"""
        return a.lerp(b, t)

    # ------------------------------------------------------------------
    # 帧更新
    # ------------------------------------------------------------------
    def update(self, dt: float):
        """每帧更新：推进状态机、恢复后坐力、插值姿态"""
        if dt <= 0:
            return
        self.time += dt

        if self._inspect_cooldown_remaining > 0.0:
            self._inspect_cooldown_remaining = max(
                0.0, self._inspect_cooldown_remaining - dt)

        if self._muzzle_flash_timer > 0.0:
            self._muzzle_flash_timer -= dt
            if self._muzzle_flash_timer <= 0.0:
                self.muzzle_flash = False

        # 后坐力随时间平滑恢复
        recovery = min(1.0, self.RECOIL_RECOVERY_SPEED * dt)
        self.recoil_pitch -= self.recoil_pitch * recovery
        self.recoil_kick -= self.recoil_kick * recovery

        # 状态推进
        self._state["time"] = self.state_time + dt
        name = self.current_state
        if name == STATE_TRANSITION and self.state_time >= self._state["duration"]:
            if self._state.get("next_state") == STATE_RELOADING:
                self._start_reloading()
            else:
                self._enter_state(STATE_IDLE)
                self.target_pose = self.idle_pose.copy()
        elif name == STATE_RELOADING and self.state_time >= self._state["duration"]:
            # 动画结束与换弹完成严格同步：同一帧填满弹匣
            self.ammo = self.magazine_size
            self._enter_state(STATE_IDLE)
            self.target_pose = self.idle_pose.copy()
        elif name == STATE_INSPECTING and self.state_time >= self._state["duration"]:
            self._enter_state(STATE_IDLE)
            self.target_pose = self.idle_pose.copy()
            self._inspect_cooldown_remaining = self.INSPECT_COOLDOWN

        # 姿态插值：当前姿态平滑逼近目标姿态（含后坐力），动作不跳变
        blend = min(1.0, self.POSE_BLEND_SPEED * dt)
        self.pose = self.pose.lerp(self._composed_target(), blend)

    def _composed_target(self) -> Pose:
        """目标姿态 + 后坐力偏移（上跳、后坐）"""
        return Pose(
            pitch=self.target_pose.pitch + self.recoil_pitch,
            yaw=self.target_pose.yaw,
            roll=self.target_pose.roll,
            x=self.target_pose.x,
            y=self.target_pose.y,
            z=self.target_pose.z - self.recoil_kick,
        )

    # ------------------------------------------------------------------
    # 其他
    # ------------------------------------------------------------------
    def set_weapon_type(self, weapon_type: str):
        if weapon_type not in WEAPON_STATS:
            raise ValueError("未知武器类型: %r" % weapon_type)
        self.weapon_type = weapon_type
        self.magazine_size = WEAPON_STATS[weapon_type]["magazine"]
        self.ammo = min(self.ammo, self.magazine_size)

    def reset(self):
        """重置状态"""
        weapon_type = self.weapon_type
        latency = self.latency
        config = self.config
        self.__init__(config)
        self.weapon_type = weapon_type
        self.latency = latency
