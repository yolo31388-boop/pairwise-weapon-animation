# 武器动画系统

## 项目描述

武器动画系统的bug修复项目。核心模块 `animation/weapon_animation.py` 存在多个已知bug，需要修复后使测试全部通过。

## 已知问题

- 检视动画只设标志位不实际播放
- 换弹动画被打断后不恢复
- 开火动画只有枪口闪光无后坐力
- 武器状态机转换无互斥无过渡无优先级
- 动画和逻辑不同步无插值

## 项目结构

```
├── animation/weapon_animation.py       # 核心模块（带bug）
├── tests/test_weapon_animation.py    # 验收测试（红态）
├── pyproject.toml          # 项目配置
└── README.md               # 说明文档
```

## 运行测试

```bash
python -m pytest tests/test_weapon_animation.py -q
```

修复目标：所有 13 个测试全部通过。

## 技术要求

- 纯Python标准库，不引入第三方依赖
- 不修改测试文件
- 保持现有API接口不变
- 修复所有已知bug
