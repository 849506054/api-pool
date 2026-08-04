# 移动端 UI 适配（Mobile Responsive）

## 断点策略

| 断点 | 目标设备 | 关键调整 |
|------|---------|---------|
| `≤768px` | 平板/大屏手机 | 端点卡片纵向布局、按钮自适应换行、表单纵向堆叠、模态框 95% 宽度 |
| `≤480px` | 小屏手机 | 更紧凑间距、更小字体/按钮、modal 全宽、统计卡片圆角缩小 |
| `≤380px` | 极小屏 | dash-stats 单列、统计 2 列、操作按钮极紧凑 |

## 核心 CSS 改动

### 全局
```css
.btn { touch-action: manipulation; -webkit-tap-highlight-color: transparent; }
```
消除移动端 300ms 点击延迟和点击高亮。

### Viewport
```html
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
```
禁止缩放，避免 iOS 双击表单输入时页面缩放。

### 端点卡片（768px 断点）
```css
.ep-header { flex-direction: column; align-items: flex-start; gap: 6px; }
.ep-actions { flex-wrap: wrap; gap: 3px; width: 100%; }
.ep-actions .btn { font-size: 10px; padding: 3px 7px; min-width: 26px; }
.ep-meta { flex-wrap: wrap; gap: 4px; font-size: 10px; }
.ep-meta span { max-width: 45%; }
```

### 统计面板
```css
/* 768px */
.dash-stats { grid-template-columns: repeat(2, 1fr); gap: 10px; }
.stats { grid-template-columns: repeat(3, 1fr); gap: 6px; }
/* 380px */
.dash-stats { grid-template-columns: 1fr; gap: 4px; }
.stats { grid-template-columns: repeat(2, 1fr); }
```

### 端点编辑表单
```css
.form-row { grid-template-columns: 1fr !important; gap: 8px; }
```

### 模态框
```css
.modal { width: 95%; padding: 16px; max-height: 90vh; overflow-y: auto; }
.form-actions { flex-direction: column; gap: 6px; }
.form-actions .btn { width: 100%; }
```

## 注意事项
- 所有 `overflow-y: auto` 的容器加了 `-webkit-overflow-scrolling: touch` 保证 iOS 顺滑滚动
- 改动全部在 `GUI_HTML` 常量中的 `<style>` 块内，无需修改 HTML 结构