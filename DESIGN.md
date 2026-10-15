---
name: ForkIQ
description: 白色导航、浅灰工作区与绿色操作的中文 GitHub fork 管理界面
colors:
  bg: "#f5f6f5"
  card: "#fff"
  surface: "#f8faf8"
  ink: "#303732"
  muted: "#66726a"
  line: "#e3e8e4"
  primary: "#078747"
  primary-hover: "#056e39"
  primary-soft: "#e8f6ed"
  green: "#078747"
  amber: "#9a600b"
  red: "#c53b3b"
  warning-soft: "#fff7e8"
  danger-soft: "#fff0ef"
  dark-bg: "#171c19"
  dark-card: "#202722"
  dark-surface: "#28302a"
  dark-ink: "#e5ede7"
  dark-muted: "#aab8ae"
  dark-line: "#364139"
  dark-primary: "#62d990"
  dark-primary-hover: "#83e7a9"
  dark-primary-soft: "#253e2d"
  dark-green: "#62d990"
  dark-amber: "#e8b66b"
  dark-red: "#ff9891"
  dark-warning-soft: "#3a3022"
  dark-danger-soft: "#3c2929"
  dark-button-text: "#132b1b"
typography:
  headline:
    fontFamily: '"Avenir Next", "PingFang SC", "Microsoft YaHei", sans-serif'
    fontSize: "20px"
    fontWeight: 600
    lineHeight: 1.4
    letterSpacing: "-.02em"
  title:
    fontFamily: '"Avenir Next", "PingFang SC", "Microsoft YaHei", sans-serif'
    fontSize: "16px"
    fontWeight: 600
    lineHeight: 1.5
  body:
    fontFamily: '"Avenir Next", "PingFang SC", "Microsoft YaHei", sans-serif'
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.65
  button:
    fontFamily: '"Avenir Next", "PingFang SC", "Microsoft YaHei", sans-serif'
    fontSize: "13px"
    fontWeight: 500
    lineHeight: 1.5
  mono:
    fontFamily: '"SFMono-Regular", Consolas, monospace'
    fontSize: "12px"
    fontWeight: 400
    lineHeight: 1.6
rounded:
  tag: "4px"
  badge: "5px"
  control: "6px"
  card: "10px"
spacing:
  compact: "8px"
  medium: "16px"
  section: "20px"
  card: "24px"
  content: "28px"
components:
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.card}"
    typography: "{typography.button}"
    rounded: "{rounded.control}"
    padding: "7px 13px"
  button-primary-hover:
    backgroundColor: "{colors.primary-hover}"
  button-secondary:
    backgroundColor: "{colors.card}"
    textColor: "{colors.ink}"
    typography: "{typography.button}"
    rounded: "{rounded.control}"
    padding: "7px 13px"
  button-secondary-hover:
    backgroundColor: "{colors.primary-soft}"
    textColor: "{colors.primary}"
  button-danger:
    backgroundColor: "{colors.card}"
    textColor: "{colors.red}"
    typography: "{typography.button}"
    rounded: "{rounded.control}"
    padding: "7px 13px"
  button-danger-hover:
    backgroundColor: "{colors.danger-soft}"
  input:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    typography: "{typography.body}"
    rounded: "{rounded.control}"
    padding: "9px 12px"
    width: "100%"
  navigation-active:
    backgroundColor: "{colors.primary-soft}"
    textColor: "{colors.primary}"
    rounded: "{rounded.control}"
    padding: "10px 16px"
  badge-synced:
    backgroundColor: "{colors.primary-soft}"
    textColor: "{colors.green}"
    rounded: "{rounded.badge}"
    padding: "3px 9px"
  card:
    backgroundColor: "{colors.card}"
    rounded: "{rounded.card}"
    padding: "24px"
---

# Design System: ForkIQ

## Overview

**Creative North Star: "白色导航与绿色操作"**

本规范记录已实现的界面，不另设品牌方向。用户指定的视觉参考为 https://kr.hx10.com:3000/#agents：浅色中性背景、白色侧栏和顶栏、绿色操作，以及居中的登录页。ForkIQ 名称和中文界面保持不变。

界面以完成管理任务为目的（Operate）。通过紧凑导航、状态标签、表格与分组表单组织内容；颜色区分操作、同步状态与错误，而非新增产品承诺。浅色为默认主题，现有主题开关提供深色变体。

**Key Characteristics:**
- 白色导航与卡片置于浅灰工作区。
- 绿色主操作、浅绿选中态与明确的红色错误反馈。
- 紧凑的中文排版、线性图标与轻微圆角。
- 居中认证卡片和适配手机的导航、仓库及事件列表。

规范值从 `app/static/app.css` 提取；组件结构依据 `app/templates/components.html`、`app/templates/shell.html`、`app/templates/auth.html` 和 `app/templates/login.html`；交互状态依据 `app/static/app.js`。frontmatter 为颜色、字体、圆角、间距和组件属性的规范层；`.impeccable/design.json` 仅补充阴影、动效、断点与可独立渲染的组件片段。

## Colors

### Primary
- `primary` 是主按钮、链接、导航图标与键盘焦点的操作绿；`primary-hover` 用于主按钮悬停。
- `primary-soft` 是当前导航、选中筛选与普通提示的浅绿底；`green` 是成功状态的语义色，当前与主色取值相同。

### Neutral
- `bg` 承载工作区与认证页；`card` 承载白色导航、卡片及次要按钮。
- `surface` 用于输入框、表头与表格悬停底色。
- `ink` 是主体文字；`muted` 是说明、辅助标签与未选中导航；`line` 是分隔线和控件边框。

错误使用 `red` 与 `danger-soft`；警告使用 `amber` 与 `warning-soft`。这些是功能状态色，不是额外品牌强调色。状态标签同时提供中文文字，不仅靠颜色表达含义。

深色主题以 `dark-*` 对应值替换同名 CSS 变量，保持布局及语义；主按钮文字另用 `dark-button-text`。不要把浅色白字直接套用到深色主题的浅绿按钮。

## Typography

正文和标题共用 frontmatter 中的中文兼容字体栈，不依赖外部字体服务。代码及长标识符使用 `mono`，并允许在必要位置换行。

- `headline` 对应全局一级标题；顶栏实际缩为（17px），手机顶栏为（16px）。
- `title` 对应卡片二级标题；页面介绍标题为（21px），手机为（19px）。
- `body` 对应正文；按钮使用 `button` 的紧凑字号和字重。
- 统计数字使用（32px / 600 / 1.25），手机为（27px）；数字使用等宽数码排布。
- 表格文字为（13px），表头与辅助说明常用（12px），状态标签为（11px / 500 / 1.5）。

## Layout

桌面为固定左侧栏加右侧工作区，顶栏随滚动保持可见。侧栏默认宽度（216px）、顶栏最小高度（64px），内容内边距使用 `spacing.content`。卡片使用 `spacing.card`，段落卡片之间使用 `spacing.section`。

- 屏幕至少（1500px）：内容内边距为（32px 36px），顶栏横向内边距为（36px）。
- 屏幕不超过（1000px）：侧栏宽度为（190px），内容与顶栏横向内边距为（22px）。
- 屏幕不超过（760px）：顶栏最小高度为（60px），内容内边距为（20px 16px）；有 JavaScript 时侧栏变成（250px）抽屉，无 JavaScript 时导航保留为顶部横向链接。
- 手机统计从五列改为三列；仓库和事件表格变成逐条网格列表，并保留字段说明。桌面表格容器允许横向滚动。

认证页使用最小满视口高度（100dvh）和双向居中。登录卡片最大宽度（400px），其他认证卡片最大宽度（460px）；桌面内边距（36px），手机为（30px 24px）。表单提交按钮占满卡片可用宽度，不添加用户名字段或新登录方式。

## Elevation & Depth

层次由浅灰工作区、白色卡片与细分隔线共同建立。卡片使用同一轻阴影，不依赖强烈悬浮效果；深色主题仅替换相应阴影值。阴影精确值见侧车 `extensions.shadows`。

手机抽屉覆盖内容，遮罩使用 `rgba(15, 28, 20, .3)`。按钮和输入框的颜色过渡为（0.18s）；抽屉位移为（0.2s ease-out）。`prefers-reduced-motion: reduce` 下禁用过渡。

## Shapes

卡片采用 `rounded.card`；按钮、输入框、导航项与提示条采用 `rounded.control`；状态标签使用 `rounded.badge`，细小标签与表格包裹层使用 `rounded.tag`。控件和表格以细边框、水平分隔线组织，不使用额外装饰性轮廓。

线性图标继承文字颜色，主按钮图标为（16px），导航图标为（18px）。实际图标来自本地 SVG，辅助图标不重复为读屏器播报。

## Components

### Buttons

主按钮是绿色实心操作，次按钮是白底细边框，危险按钮是红字细边框；悬停分别使用更深绿色、浅绿色和浅红色。常规按钮最小高度（36px），认证和行内表单提交按钮最小高度（42px）。

通用键盘焦点使用（2px）主色外框、偏移（3px）。提交后按钮变为禁用和忙碌状态，禁用透明度为（0.55）；页面恢复时移除忙碌状态。

### Inputs / Fields

输入框用浅色表面和细边框，最小高度（42px）。焦点时变为卡片背景、主色边框和（2px）浅绿外框，偏移（1px）。字段保持可见标签，错误显示在红色提示区域。

### Navigation

侧栏链接最小高度（44px），当前项使用浅绿背景、绿色文字和（600）字重，保持 `aria-current="page"`。手机抽屉使用菜单按钮、遮罩关闭、Escape 关闭和焦点约束；关闭后抽屉不可参与焦点操作。

### Chips / Status

筛选链接使用白底细边框，当前项与悬停均使用浅绿底及绿色边框。状态标签保持中文语义：成功和待同步使用绿色组，分叉、阻塞和错误使用红色组，认证事件使用警告组，其他状态使用中性组。

### External links

仓库名与上游名以文本链接直接指向 `https://github.com/<owner>/<repo>`，在仓库列表、仓库详情与概览事件中保持一致。仓库列表保留仓库名的面板内详情链接，其右侧用图标入口打开 GitHub，图标为（13px）、`rounded.tag`、白底细边框，悬停转为浅绿底与主色边框。外部链接统一使用 `target="_blank"` 与 `rel="noopener"`；名称缺失时显示 `—`，不生成空链接。

### Versions

仓库列表把版本号直接放在仓库名与上游名下方（`version-line`）：两侧合用一个两行网格（`repo-pair`，名称 / 版本，列优先填充），两侧版本行始终同层对齐，便于逐行比对本地与上游是否一致；仓库列表不再重复模式标记（手工 / 自更新 / 自动发现只在仓库详情页展示）；仓库详情用「上游版本」「fork 版本」两行展示。每侧版本由 `version_chip` 宏渲染：最近标签（绿色 `tag-version`）、12 位短 SHA（`mono`）与提交日期（`version-date`，`muted`）；片段整体换行，不在短 SHA 内部断开。标签取自各默认分支可达的最新标签；某侧没有可达标签时保留槽位，用 `tag-missing` 的灰色 `—` 占位（悬停说明原因），避免两侧形状不一致。版本信息随每次检查从同一批 GraphQL 节点更新，不额外增加请求。所有时间（最近检查、事件时间、详情页的最近成功与分叉时间）统一用 `short_time` 渲染成 `MM-DD HH:MM`，取值是 UTC，浏览器再按访客所在时区改写；因此列表、事件表和详情页读法一致，不会出现原始 ISO 串。

仓库不再与上游相连（上游被删除、fork 被提升为独立仓库）时，仓库名或上游名后追加琥珀色 `tag-warn` 标签「上游已删除」，并保留在管理列表中，不自动移除。

版本对比列（`verdict`，判定在 `app/versions.py`）只回答一个问题：本地和上游是不是同一个版本，不是的话为什么。`same` = 同一版本；`contains` = 本地领先 N 个提交（上游提交已全部并入本地，差异来自 fork 自有提交，无需更新，绿色）；`behind` = 落后上游 N 个提交（琥珀色，本地需要更新，本地短 SHA 也变琥珀提示）；`diverged` = 与上游分叉（红色，只可能出现在手工模式：上游改动无法自动合并，悬停给出引擎记录的具体原因）；`unsynced` = 不一致（检查失败、待处理或暂停，悬停带 `last_error`，此时不引用可能过期的计数）；`gone` / `pending` = 上游不可用 / 待检查。镜像模式的仓库不留自有提交，遇到上游改写历史时直接把分支对齐到上游，不停在分叉。计数来自引擎已有的 `compare(base=fork, head=upstream)`：`ahead_by` 记为 `upstream_ahead`（上游比本地多），`behind_by` 记为 `fork_ahead`（本地比上游多），随每次检查写入数据库，长期未变动的仓库在首次检查时补测一次，因此页面渲染不额外请求 GitHub；标签整体允许在文案与短 SHA 之间换行，两侧短 SHA 仍由同一行网格对齐。

### Cards / Containers

卡片使用统一背景、圆角和轻阴影。表格嵌入卡片，表头与悬停使用 `surface`。概览五项统计的整个区域是链接，分别进入全部、已同步、待同步、需关注与暂停列表；分组计数和筛选复用同一状态集合，零值仍可点击，键盘焦点框显示在卡片内侧。

仓库状态列只显示状态标签和「查看详情」，不把完整错误塞进表格。点击打开独立的原生 `dialog`，展示仓库、中文状态和错误全文；正文保留换行、长字符串可断行、超高内容内部滚动，并适配浅色、深色和手机视口。关闭按钮、Escape 和遮罩可关闭，关闭后还焦入口；阅读浮层或键盘聚焦列表链接期间暂停片段替换。无 JavaScript 或不支持 dialog 时，链接直接进入仓库详情的完整错误锚点，不裁掉定位问题所需的信息。

### Authentication

认证卡片顶部是 ForkIQ 品牌与分叉图标，中间为标题、简短说明、带标签的原生表单和绿色提交按钮。登录仍使用现有管理员密码；视觉结构不替代已有认证、CSRF 与同步流程。

## Do's and Don'ts

### Do:
- Do 保持 ForkIQ 名称、中文界面，以及白色导航、浅灰工作区和绿色操作的参考方向。
- Do 使用既有语义颜色并同时显示状态文字；错误保持可见、可定位。
- Do 保持键盘焦点、字段标签、手机导航和减少动效设置。
- Do 从实际 CSS 更新规范值，并同步更新 HTML 文档和侧车。

### Don't:
- Don't 用新宣传主张、真实账号、凭据或仓库秘密填充设计样例。
- Don't 在视觉修改中改变管理员密码登录、服务端动作、CSRF 保护或同步行为。
- Don't 将深色主题推广为默认，或把浅色主按钮白字直接复用到深色主题。
- Don't 在手机布局中隐藏状态文字、错误内容或必要操作。
