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
    WEAPON_STATS,
    STATE_PRIORITY,
    INSPECT_DURATION,
    INSPECT_COOLDOWN,
    TRANSITION_DURATION,
    MAX_RECOIL,
)


class TestWeaponAnimation:
    """武器动画系统测试套件"""

    def setup_method(self):
        """每个测试前的初始化"""
        self.system = WeaponAnimation()

    def _fire_rounds(self, system, count):
        """以合法射速打 count 发子弹"""
        for _ in range(count):
            assert system.fire_animation() is True
            system.update(WEAPON_STATS[system.weapon_type]["fire_interval"] + 0.01)

    def test_case_01(self):
        """检视动画实际播放：有动画实例、进度随时间推进、播完回到空闲"""
        assert self.system.play_inspect() is True
        assert self.system.state == "inspect"
        # 不是只设标志位：存在真实动画实例
        anim = self.system.current_animation
        assert anim is not None
        assert anim["name"] == "inspect_" + self.system.weapon_type
        assert anim["duration"] == INSPECT_DURATION
        # 进度随 update 推进
        self.system.update(1.0)
        assert 0.0 < self.system.animation_progress < 1.0
        # 播完整个时长后经收枪过渡回到空闲
        self.system.update(INSPECT_DURATION - 1.0)
        assert self.system.state == "transition"
        self.system.update(TRANSITION_DURATION)
        assert self.system.state == "idle"

    def test_case_02(self):
        """检视动画不可打断：播放期间开火/换弹/打断均无效，状态不错乱"""
        assert self.system.play_inspect() is True
        assert self.system.fire_animation() is False
        assert self.system.reload_animation() is False
        assert self.system.interrupt() is False
        assert self.system.state == "inspect"
        # 弹药等逻辑状态未错乱
        assert self.system.ammo == WEAPON_STATS["rifle"]["magazine"]
        # 检视仍能正常播完
        self.system.update(INSPECT_DURATION + TRANSITION_DURATION)
        assert self.system.state == "idle"

    def test_case_03(self):
        """检视动画有冷却：播放中及结束后冷却期内不能反复刷"""
        assert self.system.play_inspect() is True
        assert self.system.play_inspect() is False  # 播放中重复按无效
        self.system.update(INSPECT_DURATION + TRANSITION_DURATION)
        assert self.system.state == "idle"
        assert self.system.inspect_cooldown_remaining > 0.0
        assert self.system.play_inspect() is False  # 冷却中
        self.system.update(INSPECT_COOLDOWN)
        assert self.system.inspect_cooldown_remaining == 0.0
        assert self.system.play_inspect() is True  # 冷却结束可再次检视

    def test_case_04(self):
        """换弹动画被打断后恢复：武器不卡在中间状态，弹药不被错误填满"""
        self._fire_rounds(self.system, 3)
        ammo_before = self.system.ammo
        assert self.system.reload_animation() is True
        self.system.update(0.5)  # 换弹进行到一半
        assert self.system.interrupt() is True
        # 经收枪过渡恢复到稳定空闲状态
        self.system.update(TRANSITION_DURATION)
        assert self.system.state == "idle"
        # 弹药保持打断前的数量，未填满也未丢失
        assert self.system.ammo == ammo_before
        # 武器姿态恢复举起，且可以正常继续操作
        self.system.update(0.5)
        assert self.system.get_pose()["raise"] > 0.9
        assert self.system.fire_animation() is True

    def test_case_05(self):
        """换弹动画时长与实际换弹时间匹配，且按武器类型区分"""
        for weapon_type, stats in WEAPON_STATS.items():
            system = WeaponAnimation({"weapon_type": weapon_type})
            self._fire_rounds(system, 1)
            assert system.reload_animation() is True
            # 动画时长 == 该武器实际换弹时间
            assert system.state_duration == stats["reload_time"]
            assert system.current_animation["name"] == "reload_" + weapon_type
            assert system.current_animation["duration"] == stats["reload_time"]
            # 动画播完前弹药不填，播完瞬间才填满（动画与逻辑同步）
            system.update(stats["reload_time"] - 0.01)
            assert system.ammo == stats["magazine"] - 1
            system.update(0.02)
            assert system.ammo == stats["magazine"]
        # 不同武器类型的换弹时长确实不同
        durations = {stats["reload_time"] for stats in WEAPON_STATS.values()}
        assert len(durations) == len(WEAPON_STATS)

    def test_case_06(self):
        """开火动画有后坐力：武器上跳并平滑恢复，同时有枪口闪光"""
        assert self.system.fire_animation() is True
        assert self.system.recoil > 0.0  # 后坐力（武器上跳）
        assert self.system.muzzle_flash_active  # 枪口闪光
        pose = self.system.get_pose()
        assert pose["recoil"] > 0.0
        assert pose["muzzle_flash"] is True
        # 后坐力随时间平滑恢复
        recoil_before = self.system.recoil
        self.system.update(0.05)
        assert 0.0 <= self.system.recoil < recoil_before

    def test_case_07(self):
        """开火动画考虑射速：不能超射速射击，连续射击后坐力叠加上限封顶"""
        interval = WEAPON_STATS["rifle"]["fire_interval"]
        assert self.system.fire_animation() is True
        assert self.system.fire_animation() is False  # 射速限制，不能立刻再射
        self.system.update(interval)
        recoil_before = self.system.recoil
        assert self.system.fire_animation() is True
        assert self.system.recoil > recoil_before  # 连续射击后坐力叠加
        # 持续连射，后坐力叠加有上限
        for _ in range(50):
            self.system.update(interval)
            self.system.fire_animation()
        assert self.system.recoil <= MAX_RECOIL

    def test_case_08(self):
        """开火动画按武器类型区分：后坐力与射速因武器而异"""
        pistol = WeaponAnimation({"weapon_type": "pistol"})
        sniper = WeaponAnimation({"weapon_type": "sniper"})
        assert pistol.fire_animation() is True
        assert sniper.fire_animation() is True
        # 狙击枪后坐力远大于手枪
        assert sniper.recoil > pistol.recoil
        assert sniper.recoil == WEAPON_STATS["sniper"]["recoil"]
        assert pistol.recoil == WEAPON_STATS["pistol"]["recoil"]
        # 射速不同：0.3 秒后手枪可再射，狙击枪不行
        pistol.update(0.3)
        sniper.update(0.3)
        assert pistol.fire_animation() is True
        assert sniper.fire_animation() is False

    def test_case_09(self):
        """武器状态机互斥：换弹中不能同时开火或检视"""
        assert self.system.reload_animation() is True
        assert self.system.fire_animation() is False
        assert self.system.play_inspect() is False
        # 状态是单一值，不会同时处于换弹和开火
        machine = self.system.weapon_state_machine()
        assert machine["state"] == "reload"
        assert self.system.state == "reload"

    def test_case_10(self):
        """武器状态机有过渡：换弹结束经收枪动作才能开火"""
        assert self.system.reload_animation() is True
        self.system.update(WEAPON_STATS["rifle"]["reload_time"])
        # 不是直接变空闲/开火，而是先进入收枪过渡
        assert self.system.state == "transition"
        assert self.system.fire_animation() is False  # 过渡中不能开火
        self.system.update(TRANSITION_DURATION)
        assert self.system.state == "idle"
        assert self.system.fire_animation() is True  # 过渡完成后可以开火

    def test_case_11(self):
        """武器状态机有优先级：低优先级不能打断高优先级"""
        assert STATE_PRIORITY["reload"] > STATE_PRIORITY["inspect"]
        # 换弹（高优先级）进行中，检视（低优先级）请求被拒绝
        assert self.system.reload_animation() is True
        assert self.system.play_inspect() is False
        assert self.system.state == "reload"
        # 反向：换弹可以打断开火（高优先级打断低优先级）
        other = WeaponAnimation()
        assert other.fire_animation() is True
        assert other.reload_animation() is True
        assert other.state == "reload"
        # 检视不可被任何动作打断
        third = WeaponAnimation()
        assert third.play_inspect() is True
        assert third.reload_animation() is False
        assert third.state == "inspect"

    def test_case_12(self):
        """动画与逻辑同步：开火即扣弹药，换弹动画播完瞬间才填弹"""
        magazine = WEAPON_STATS["rifle"]["magazine"]
        assert self.system.fire_animation() is True
        assert self.system.ammo == magazine - 1  # 开火动画与弹药逻辑同步
        # 同步快照中动画状态与逻辑状态一致
        snapshot = self.system.animation_sync()
        assert snapshot["ammo"] == self.system.ammo
        assert snapshot["state"] == self.system.state
        assert snapshot["weapon_type"] == self.system.weapon_type
        # 换弹动画未播完时弹匣不填，播完才填
        assert self.system.reload_animation() is True
        self.system.update(WEAPON_STATS["rifle"]["reload_time"] - 0.01)
        assert self.system.ammo == magazine - 1
        self.system.update(0.02)
        assert self.system.ammo == magazine

    def test_case_13(self):
        """动画同步考虑延迟与插值：远端状态经延迟补偿后一致，姿态平滑不跳变"""
        # 插值：姿态向目标平滑逼近，不是瞬间跳变
        assert self.system.reload_animation() is True  # 目标姿态降低（收枪换弹）
        pose_before = self.system.get_pose()["raise"]
        self.system.update(0.01)
        pose_after = self.system.get_pose()["raise"]
        assert 0.4 < pose_after < pose_before  # 介于起点与目标之间，未跳变
        # 延迟补偿：快照传输期间换弹已完成，远端应结算到一致状态
        source = WeaponAnimation()
        source.reload_animation()
        source.update(1.0)
        snapshot = source.animation_sync()
        remote = WeaponAnimation()
        remote.apply_sync(snapshot, latency=1.5)  # 1.0 + 1.5 > 2.0 换弹时长
        assert remote.state in ("transition", "idle")
        assert remote.ammo == WEAPON_STATS["rifle"]["magazine"]  # 弹药逻辑已同步
        # 远端姿态同样经插值平滑恢复
        remote.update(0.5)
        assert remote.get_pose()["raise"] > 0.5


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
