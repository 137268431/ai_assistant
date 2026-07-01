# TradingView new layout

这个目录按职责拆成三层，避免 Core / Display 主文件继续膨胀，也避免 Display 的影子推演被误认为真实成交。

## 目录职责

- `core/`: TradingView `strategy()` 主脚本，只负责真实下单、`strategy.entry/exit/close`、alert payload、真实成交/退出标签。
- `core/libs/`: Core 专用库，例如 payload 拼接、真实退出标签文本/颜色/绘制；Display 不要引用。
- `display/`: TradingView `indicator()` 主脚本，只负责可视化说明、计划线、诊断 marker、Display shadow 推演。
- `display/libs/`: Display 专用库，例如 label 样式/坐标、Display 文案；Core 不要引用。
- `common/libs/`: Core 和 Display 都会用到的纯工具库，例如格式化、风控计算、结构 level、模型选择。

## 对齐规则

- 真实 `RUN/TP/SL/EXIT` 只由 `core/Signal_Strategy_Core[Glory].pine` 基于 `strategy.closedtrades` 输出。
- Core 退出价格使用 `strategy.closedtrades.exit_price()`，退出 bar 使用 `strategy.closedtrades.exit_bar_index()`，避免用当前 K 的 stop/target 猜测导致标签偏移。
- Display 的 shadow trade lifecycle 默认关闭；需要调试时打开 `Show SHADOW trade lifecycle labels`，标签会带 `SHADOW` 前缀。
- Display shadow 只能解释“如果按 Display 推演会怎样”，不能当作真实成交或真实退出依据。

## 发布顺序

1. 先发布 `common/libs/*.pine`。
2. 再发布 `core/libs/*.pine` 和 `display/libs/*.pine`。
3. 最后发布 `core/Signal_Strategy_Core[Glory].pine` 与 `display/Signal_Strategy_Display[Glory].pine`。

如果库版本升级，主脚本里的 `import o8431/.../<version>` 需要同步更新。
