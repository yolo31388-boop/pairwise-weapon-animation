"""
武器动画系统 - 验收测试

运行方式：python -m pytest tests/test_weapon_animation.py -q
共 13 个测试用例
"""
import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from animation.weapon_animation import (
    WeaponAnimation,
    Pose,
    WEAPON_STATS,
    STATE_IDLE,
    STATE_TRANSITION,
    STATE_RELOADING,
    STATE_INSPECTING,
)


class TestWeaponAnimation:
    """武器动画系统测试套件"""

    def setup_method(self):
        """每个测试前的初始化"""
        self.system = WeaponAnimation()

    def _enter_reloading(self, system):
        """辅助：请求换弹并推进到 reloading 状态"""
        assert system.reload_animation() is True
        assert system.current_state == STATE_TRANSITION
        system.update(system.TRANSITION_DURATION)
        assert system.current_state == STATE_RELOADING

    def test_case_01(self):
        """检视动画实际播放：进入检视状态、武器全程可见、进度随时间推进"""
        assert self.system.play_inspect() is True
        assert self.system.current_state == STATE_INSPECTING
        assert self.system.is_weapon_visible() is True
        self.system.update(1.0)
        assert self.system.current_state == STATE_INSPECTING
        assert 0.0 < self.system.inspect_progress() < 1.0
        assert self.system.is_weapon_visible() is True
        self.system.update(self.system.INSPECT_DURATION)
        assert self.system.current_state == STATE_IDLE

    def test_case_02(self):
        """检视动画不可打断：检视期间开火/换弹请求均被拒绝"""
        assert self.system.play_inspect() is True
        assert self.system.fire_animation() is False
        assert self.system.reload_animation() is False
        assert self.system.current_state == STATE_INSPECTING
        self.system.update(1.0)
        assert self.system.current_state == STATE_INSPECTING

    def test_case_03(self):
        """检视动画有冷却：播放中重复按键无效，结束后冷却期内不可再次检视"""
        assert self.system.play_inspect() is True
        assert self.system.play_inspect() is False  # 播放中重复按
        self.system.update(self.system.INSPECT_DURATION + 0.01)
        assert self.system.current_state == STATE_IDLE
        assert self.system.inspect_cooldown_remaining > 0.0
        assert self.system.play_inspect() is False  # 冷却中
        self.system.update(self.system.INSPECT_COOLDOWN)
        assert self.system.play_inspect() is True   # 冷却结束

    def test_case_04(self):
        """换弹动画时长与实际换弹时间匹配：动画播完同一帧换弹完成"""
        self.system.ammo = 3
        self._enter_reloading(self.system)
        duration = WEAPON_STATS["rifle"]["reload_time"]
        self.system.update(duration / 2)
        assert self.system.current_state == STATE_RELOADING
        assert self.system.reload_progress() == pytest.approx(0.5)
        assert self.system.ammo == 3  # 动画没播完，弹匣还没换
        self.system.update(duration / 2)
        assert self.system.current_state == STATE_IDLE
        assert self.system.ammo == self.system.magazine_size  # 播完即完成

    def test_case_05(self):
        """换弹动画被打断后恢复：武器回到待机姿态，不卡在半空，弹药不变"""
        self.system.ammo = 3
        self._enter_reloading(self.system)
        self.system.update(0.5)
        assert self.system.target_pose.pitch != 0.0  # 武器处于换弹姿态
        assert self.system.interrupt_reload() is True
        assert self.system.current_state == STATE_IDLE
        assert self.system.pose.pitch == 0.0 and self.system.pose.y == 0.0
        assert self.system.target_pose.pitch == 0.0
        assert self.system.ammo == 3  # 打断不白送弹药
        assert self.system.reload_animation() is True  # 可重新换弹

    def test_case_06(self):
        """换弹动画按武器类型区分：狙击枪换弹比手枪慢"""
        pistol = WeaponAnimation({"weapon_type": "pistol"})
        sniper = WeaponAnimation({"weapon_type": "sniper"})
        assert pistol.reload_duration() == WEAPON_STATS["pistol"]["reload_time"]
        assert sniper.reload_duration() == WEAPON_STATS["sniper"]["reload_time"]
        assert sniper.reload_duration() > pistol.reload_duration()
        # 实际状态机使用的时长也随武器类型变化
        self._enter_reloading(pistol)
        self._enter_reloading(sniper)
        pistol.update(WEAPON_STATS["pistol"]["reload_time"])
        assert pistol.current_state == STATE_IDLE
        assert sniper.current_state == STATE_RELOADING  # 狙击枪还没换完

    def test_case_07(self):
        """开火动画有后坐力：武器上跳并随时间恢复，不只有枪口闪光"""
        assert self.system.fire_animation() is True
        assert self.system.muzzle_flash is True
        assert self.system.recoil_pitch > 0.0  # 武器上跳
        self.system.update(0.016)
        assert self.system.pose.pitch > 0.0   # 姿态确实抬起
        pitch_after_recoil = self.system.recoil_pitch
        self.system.update(0.5)
        assert self.system.recoil_pitch < pitch_after_recoil  # 后坐力恢复
        self.system.update(5.0)
        assert self.system.recoil_pitch == pytest.approx(0.0)

    def test_case_08(self):
        """开火动画考虑射速：间隔不足的射击被丢弃，后坐力叠加有上限"""
        rifle_interval = WEAPON_STATS["rifle"]["fire_interval"]
        assert self.system.fire_animation() is True
        # 不推进时间连续扣扳机：全部被射速限制拒绝，动画不叠加
        for _ in range(10):
            assert self.system.fire_animation() is False
        assert self.system.ammo == self.system.magazine_size - 1
        # 推进超过射速间隔后可以再次开火
        self.system.update(rifle_interval + 0.01)
        assert self.system.fire_animation() is True
        # 连续射击后坐力叠加但有上限，不会无限上天
        max_pitch = WEAPON_STATS["rifle"]["recoil_pitch"] * self.system.MAX_RECOIL_STACK
        for _ in range(20):
            self.system.update(rifle_interval + 0.01)
            self.system.fire_animation()
            assert self.system.recoil_pitch <= max_pitch + 1e-9

    def test_case_09(self):
        """开火动画按武器类型区分：狙击枪后坐力远大于手枪"""
        pistol = WeaponAnimation({"weapon_type": "pistol"})
        sniper = WeaponAnimation({"weapon_type": "sniper"})
        assert pistol.fire_animation() is True
        assert sniper.fire_animation() is True
        assert sniper.recoil_pitch > pistol.recoil_pitch
        assert sniper.recoil_pitch == WEAPON_STATS["sniper"]["recoil_pitch"]
        assert pistol.recoil_pitch == WEAPON_STATS["pistol"]["recoil_pitch"]

    def test_case_10(self):
        """状态机互斥：换弹与开火不能同时处于激活状态"""
        self.system.ammo = 3
        self._enter_reloading(self.system)
        # 开火优先级更高：打断换弹并恢复，而不是两个状态并存
        assert self.system.fire_animation() is True
        assert self.system.current_state == STATE_IDLE
        assert self.system.current_state != STATE_RELOADING
        assert self.system.ammo == 2  # 换弹被打断未填满，且这一枪消耗一发
        # 检视中开火被拒绝，不会叠加状态
        assert self.system.play_inspect() is True
        assert self.system.fire_animation() is False
        assert self.system.current_state == STATE_INSPECTING

    def test_case_11(self):
        """状态机有过渡：换弹先经过收枪过渡，不是直接从待机变换弹"""
        assert self.system.reload_animation() is True
        assert self.system.current_state == STATE_TRANSITION  # 收枪动作
        self.system.update(self.system.TRANSITION_DURATION / 2)
        assert self.system.current_state == STATE_TRANSITION
        self.system.update(self.system.TRANSITION_DURATION / 2)
        assert self.system.current_state == STATE_RELOADING
        history = self.system.weapon_state_machine()["history"]
        states = [to for _, to in history]
        assert states.index(STATE_TRANSITION) < states.index(STATE_RELOADING)

    def test_case_12(self):
        """状态机有优先级：低优先级动画不能打断高优先级状态"""
        self.system.ammo = 3
        self._enter_reloading(self.system)
        # 检视优先级低于换弹，不能打断换弹
        assert self.system.play_inspect() is False
        assert self.system.current_state == STATE_RELOADING
        # 过渡动画播放期间不接受任何新请求
        self.system.interrupt_reload()
        assert self.system.reload_animation() is True
        assert self.system.play_inspect() is False
        assert self.system.current_state == STATE_TRANSITION

    def test_case_13(self):
        """动画同步：进度按网络延迟前向预测，姿态对齐走插值不跳变"""
        self.system.ammo = 3
        self._enter_reloading(self.system)
        elapsed = 0.5
        self.system.update(elapsed)
        latency = 0.1
        packet = self.system.animation_sync(latency=latency)
        duration = WEAPON_STATS["rifle"]["reload_time"]
        # 同步包进度包含延迟补偿，远端看到的与实际状态一致
        assert packet["progress"] == pytest.approx((elapsed + latency) / duration)
        assert packet["timestamp"] == pytest.approx(self.system.time + latency)
        # 远端姿态对齐：设置为目标姿态，由 update 插值逼近
        remote_pose = Pose(pitch=40.0)
        before = self.system.pose.pitch
        self.system.animation_sync(remote_state={"pose": remote_pose})
        assert self.system.pose.pitch == before  # 没有跳变
        self.system.update(0.016)
        assert before < self.system.pose.pitch < 40.0  # 插值向远端姿态逼近
        # 插值函数本身：中点取半
        mid = WeaponAnimation.interpolate(Pose(), Pose(pitch=10.0), 0.5)
        assert mid.pitch == pytest.approx(5.0)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
