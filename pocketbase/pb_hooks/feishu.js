/**
 * feishu.js - 飞书通知共享模块
 * 通过 require(`${__hooks}/feishu.js`) 引入
 */

// 三个独立的 webhook：信号、订单、异常
var FEISHU_WEBHOOK_URL_SIGNAL = "https://open.feishu.cn/open-apis/bot/v2/hook/298ce054-9666-4a30-a59b-b14ac0faa8f1";
var FEISHU_WEBHOOK_URL_ORDER = "https://open.feishu.cn/open-apis/bot/v2/hook/eee38484-b055-483b-87c8-b918ec8a178d";
var FEISHU_WEBHOOK_URL_ERROR = "https://open.feishu.cn/open-apis/bot/v2/hook/98f6f9a5-5d5a-4974-b363-5bc6384aadd4";
var FEISHU_WEBHOOK_URL = FEISHU_WEBHOOK_URL_SIGNAL;  // 默认用信号 webhook

function sendFeishuText(text, atAll = true) {
  // 使用 post 富文本类型，at tag 元素实现 @所有人
  const contentLine = [];
  if (atAll) {
    contentLine.push({ tag: "at", user_id: "all" });
  }
  contentLine.push({ tag: "text", text: (atAll ? " " : "") + text });

  try {
    const res = $http.send({
      url: FEISHU_WEBHOOK_URL,
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        msg_type: "post",
        content: {
          post: {
            zh_cn: {
              title: "",
              content: [contentLine]
            }
          }
        }
      }),
      timeout: 10
    });
    if (res.statusCode !== 200) {
      console.error("[Feishu] 发送失败:", res.statusCode, res.raw);
      return false;
    }
    console.log("[Feishu] 文本消息发送成功");
    return true;
  } catch (err) {
    console.error("[Feishu] 发送异常:", err);
    return false;
  }
}

function sendFeishuPost(title, content, type = "signal") {
  // 根据 type 选择不同的 webhook
  var webhookUrl = FEISHU_WEBHOOK_URL_SIGNAL;
  if (type === "order") {
    webhookUrl = FEISHU_WEBHOOK_URL_ORDER;
  } else if (type === "error") {
    webhookUrl = FEISHU_WEBHOOK_URL_ERROR;
  }

  try {
    const res = $http.send({
      url: webhookUrl,
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        msg_type: "post",
        content: { post: { zh_cn: { title: title, content: content } } }
      }),
      timeout: 10
    });
    if (res.statusCode !== 200) {
      console.error("[Feishu] 发送失败:", res.statusCode, res.raw);
      return false;
    }
    console.log("[Feishu] 消息发送成功:", title, "| 响应:", res.raw);
    return true;
  } catch (err) {
    console.error("[Feishu] 发送异常:", err);
    return false;
  }
}

var PB_HOST = "https://pb.lzw-glory.top";
var SIGNAL_ACTION_TOKEN = (function() {
  try {
    const cfg = $app.findFirstRecordByFilter("config", "key = 'signal_action_token'");
    const token = cfg ? cfg.get("value") || "" : "";
    console.log(`[Feishu] signal_action_token 配置读取结果: ${token ? '已配置' : '未配置'}`);
    return token;
  } catch(e) {
    console.log(`[Feishu] signal_action_token 配置读取失败: ${e}`);
    return "";
  }
})();

function notifyNewSignal(signal) {
  const directionText = signal.direction === "long" ? "做多 📈" : "做空 📉";
  const color = signal.direction === "long" ? "green" : "red";
  const tokenParam = SIGNAL_ACTION_TOKEN ? "&token=" + SIGNAL_ACTION_TOKEN : "";

  const extra = signal.extra || {};

  // TODO: 接入 OpenClaw webhook 分析信号

  // 大盘关联信息 - 直接显示 SPY 和 QQQ 涨跌幅
  let marketInfoText = "";
  if (signal.market_indexes && signal.market_indexes.length > 0) {
    // 提取 SPY 和 QQQ 数据
    const spyData = signal.market_indexes.find(m => m.symbol === 'SPY');
    const qqqData = signal.market_indexes.find(m => m.symbol === 'QQQ');
    const vixData = signal.market_indexes.find(m => m.symbol === 'VIX');

    let parts = [];
    if (spyData) {
      const change = spyData.change_pct;
      parts.push(`SPY: ${change > 0 ? '+' : ''}${change.toFixed(2)}%`);
    }
    if (qqqData) {
      const change = qqqData.change_pct;
      parts.push(`QQQ: ${change > 0 ? '+' : ''}${change.toFixed(2)}%`);
    }
    if (parts.length > 0) {
      marketInfoText = `**大盘:** ${parts.join(" | ")}`;
    }

    // VIX 恐慌指数单独一行
    if (vixData) {
      const vixLevel = vixData.change_pct >= 20 ? "🔴 恐慌" : vixData.change_pct >= 10 ? "🟡 紧张" : "🟢 平稳";
      const vixText = `\n**VIX恐慌:** ${vixLevel} ${vixData.change_pct > 0 ? '+' : ''}${vixData.change_pct.toFixed(1)}%`;
      marketInfoText += vixText;
    }
  }

  // 计算止盈止损对应的盈亏金额（乘以股数）
  const isLong = signal.direction === "long";
  const shares = signal.shares || 0;
  const tpProfit = (isLong ? (signal.take_profit - signal.entry) : (signal.entry - signal.take_profit)) * shares;
  const slLoss = (isLong ? (signal.entry - signal.stop_loss) : (signal.stop_loss - signal.entry)) * shares;

  // 格式化金额函数（转换为 w 单位）
  function formatAmount(amount) {
    if (!amount || amount === 0) return "0";
    if (Math.abs(amount) >= 10000) {
      return (amount / 10000).toFixed(2) + "w";
    }
    return amount.toFixed(2);
  }

  // 构建两列布局的字段 - 左侧
  const leftColumn = [];
  leftColumn.push({ tag: "div", text: { tag: "lark_md", content: "**标的:** " + signal.symbol } });
  leftColumn.push({ tag: "div", text: { tag: "lark_md", content: "**方向:** " + directionText } });

  // 涨幅（当日/前收/近7日）
  let changeDisplay = "N/A";
  if (extra.day_change_pct !== undefined) {
    const dayPct = Number(extra.day_change_pct || 0);
    const prevPct = Number(extra.prev_close_change_pct || 0);
    const d7Pct = Number(extra.change_7d || 0);
    changeDisplay = `${dayPct > 0 ? '+' : ''}${dayPct.toFixed(2)}%/${prevPct > 0 ? '+' : ''}${prevPct.toFixed(2)}%/${d7Pct > 0 ? '+' : ''}${d7Pct.toFixed(2)}%`;
  }
  leftColumn.push({ tag: "div", text: { tag: "lark_md", content: "**涨幅:** " + changeDisplay } });
  leftColumn.push({ tag: "div", text: { tag: "lark_md", content: "**入场:** $" + signal.entry.toFixed(2) } });
  leftColumn.push({ tag: "div", text: { tag: "lark_md", content: "**止盈:** $" + signal.take_profit.toFixed(2) } });
  leftColumn.push({ tag: "div", text: { tag: "lark_md", content: "**止损:** $" + signal.stop_loss.toFixed(2) } });

  // 构建两列布局的字段 - 右侧
  const rightColumn = [];
  rightColumn.push({ tag: "div", text: { tag: "lark_md", content: "**盈利:** +$" + formatAmount(tpProfit) } });
  rightColumn.push({ tag: "div", text: { tag: "lark_md", content: "**亏损:** -$" + formatAmount(slLoss) } });
  rightColumn.push({ tag: "div", text: { tag: "lark_md", content: "**风报比:** " + (signal.rr || "N/A") } });
  rightColumn.push({ tag: "div", text: { tag: "lark_md", content: "**股数:** " + (signal.shares || "N/A") } });

  // 波动率和 ATR 止损（股数下面）
  if (extra.atr_pct) {
    const atrLevel = extra.atr_pct >= 3 ? "高" : extra.atr_pct >= 1.5 ? "中" : "低";
    const atrEmoji = extra.atr_pct >= 3 ? "⚡" : extra.atr_pct >= 1.5 ? "〜" : "·";
    rightColumn.push({ tag: "div", text: { tag: "lark_md", content: `**波动率:** ${atrEmoji} ${atrLevel} ${extra.atr_pct.toFixed(2)}%` } });
    if (extra.sl_atr_ratio) {
      rightColumn.push({ tag: "div", text: { tag: "lark_md", content: `**ATR止损:** ${extra.sl_atr_ratio.toFixed(1)}倍` } });
    }
  } else if (extra.atr) {
    rightColumn.push({ tag: "div", text: { tag: "lark_md", content: "**ATR:** " + extra.atr.toFixed(2) } });
  }

  // 大盘信息
  let marketColumn = null;
  if (marketInfoText) {
    marketColumn = { tag: "div", text: { tag: "lark_md", content: marketInfoText.replace(/^\n/, "") } };
  }

  // 添加原因（单独一行）
  let reasonColumn = null;
  if (extra.reason) {
    reasonColumn = { tag: "div", text: { tag: "lark_md", content: "**原因:** " + extra.reason } };
  }

  // 添加信号ID（单独一行）
  const signalIdColumn = { tag: "div", text: { tag: "lark_md", content: "**信号ID:** " + signal.signal_id } };

  // 构建 elements 数组
  const elements = [
    {
      tag: "column_set",
      columns: [
        { tag: "column", width: "weighted", weight: 1, elements: leftColumn },
        { tag: "column", width: "weighted", weight: 1, elements: rightColumn }
      ]
    }
  ];

  // 添加原因行（如果存在）
  if (reasonColumn) {
    elements.push({
      tag: "column_set",
      columns: [
        { tag: "column", width: "weighted", weight: 1, elements: [reasonColumn] }
      ]
    });
  }

  // 添加信号ID行
  elements.push({
    tag: "column_set",
    columns: [
      { tag: "column", width: "weighted", weight: 1, elements: [signalIdColumn] }
    ]
  });

  // 添加大盘信息行（如果存在）
  if (marketColumn) {
    elements.push({
      tag: "column_set",
      columns: [
        { tag: "column", width: "weighted", weight: 1, elements: [marketColumn] }
      ]
    });
  }

  const card = {
    header: {
      title: { tag: "plain_text", content: "🔔 新交易信号 · " + signal.symbol + " · " + (signal.us_time || "") },
      template: color
    },
    elements: elements
  };

  // 添加操作按钮（使用回调模式，点击后更新卡片内容）
  card.elements.push({
    tag: "action",
    actions: [
      {
        tag: "button",
        text: { tag: "plain_text", content: "✅ 确认" },
        type: "primary",
        action_type: "callback",
        url: PB_HOST + "/webhook/feishu/callback",
        value: { action: "confirm", signal_id: signal.signal_id }
      },
      {
        tag: "button",
        text: { tag: "plain_text", content: "❌ 拒绝" },
        type: "danger",
        action_type: "callback",
        url: PB_HOST + "/webhook/feishu/callback",
        value: { action: "reject", signal_id: signal.signal_id }
      }
    ]
  });

  try {
    const res = $http.send({
      url: FEISHU_WEBHOOK_URL,
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ msg_type: "interactive", card: card }),
      timeout: 10
    });
    console.log("[Feishu] 信号通知发送:", res.raw);
  } catch (err) {
    console.error("[Feishu] 信号通知失败:", err);
  }
}

// 订单创建时发送带操作按钮的交互卡片
function notifyNewOrder(order) {
  const directionText = order.direction === "long" ? "做多 📈" : "做空 📉";
  const color = order.direction === "long" ? "green" : "red";
  const tokenParam = SIGNAL_ACTION_TOKEN ? "&token=" + SIGNAL_ACTION_TOKEN : "";

  let fields = "**标的:** " + order.symbol + "\n" +
    "**方向:** " + directionText + "\n" +
    "**类型:** " + order.order_type + "\n" +
    "**数量:** " + (order.quantity || "N/A") + "\n" +
    "**限价:** $" + (order.limit_price ? order.limit_price.toFixed(2) : "N/A");
  if (order.signal_id) fields += "\n**信号ID:** " + order.signal_id;
  fields += "\n**订单ID:** " + order.unique_id;

  const card = {
    header: {
      title: { tag: "plain_text", content: "📝 新订单 · " + order.symbol },
      template: color
    },
    elements: [
      { tag: "div", text: { tag: "lark_md", content: fields } },
      {
        tag: "action",
        actions: [
          {
            tag: "button",
            text: { tag: "plain_text", content: "❌ 取消挂单" },
            type: "danger",
            action_type: "callback",
            url: PB_HOST + "/webhook/feishu/order/callback",
            value: { action: "cancel", order_id: order.unique_id }
          },
          {
            tag: "button",
            text: { tag: "plain_text", content: "🔒 平仓" },
            type: "default",
            action_type: "callback",
            url: PB_HOST + "/webhook/feishu/order/callback",
            value: { action: "close", order_id: order.unique_id }
          }
        ]
      }
    ]
  };

  try {
    const res = $http.send({
      url: FEISHU_WEBHOOK_URL,
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ msg_type: "interactive", card: card }),
      timeout: 10
    });
    console.log("[Feishu] 订单通知发送:", res.raw);
  } catch (err) {
    console.error("[Feishu] 订单通知失败:", err);
  }
}

// 订单状态变更时发送普通通知
function notifyOrder(action, order) {
  const titles = {
    filled:   "✅ 订单已成交",
    canceled: "❌ 订单已取消",
    closed:   "🔒 订单已平仓",
    updated:  "🔄 订单状态变更"
  };
  const title = (titles[action] || "📊 订单状态变更") + " · " + order.symbol;
  const directionText = order.direction === "long" ? "做多 📈" : "做空 📉";
  const content = [
    [{ tag: "text", text: "标的: " + order.symbol }],
    [{ tag: "text", text: "方向: " + directionText }],
    [{ tag: "text", text: "类型: " + order.order_type }],
    [{ tag: "text", text: "状态: " + order.status }]
  ];
  if (order.quantity)    content.push([{ tag: "text", text: "数量: " + order.quantity }]);
  if (order.limit_price) content.push([{ tag: "text", text: "限价: $" + order.limit_price.toFixed(2) }]);
  if (order.fill_price)  content.push([{ tag: "text", text: "成交价: $" + order.fill_price.toFixed(2) }]);
  if (order.pnl !== undefined) content.push([{ tag: "text", text: "盈亏: $" + Number(order.pnl).toFixed(2) }]);
  if (order.signal_id)   content.push([{ tag: "text", text: "信号ID: " + order.signal_id }]);
  content.push([{ tag: "text", text: "订单ID: " + order.unique_id }]);
  sendFeishuPost(title, content, "order");
}

function notifySimple(message, atAll = true) {
  sendFeishuText(message, atAll);
}

module.exports = { sendFeishuText, sendFeishuPost, notifyNewSignal, notifyNewOrder, notifyOrder, notifySimple }
