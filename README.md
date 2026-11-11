# 上游流域降雨量数据

这是一个公开的静态数据网页，目标为云南16个地区展示分时段实测降雨量。

## 已上传内容
- `index.html`：网页表格和数据读取
- `data/rainfall.json`：数据文件
- `scripts/collect.py`：采集脚本安全占位
- `.github/workflows/rainfall.yml`：每天两次定时运行框架

## 重要：实测数据接口尚未接入
目前没有确认云南天气网提供可稳定访问的16地实测累计降雨接口及站点映射。因此采集脚本不会填充0或伪造数值，网页空白表示暂无经过核实的数据。城市天气预报页面不能当作历史实测雨量来源。

## 统计口径
- 早间：前一日21:00—当日07:00
- 晚间：当日08:00—20:00
- 晚间最终值需要用24小时总量减早间值进行校验。

## 启用 GitHub Pages
仓库 Settings -> Pages -> Build and deployment：
1. Source 选择 Deploy from a branch
2. Branch 选择 main
3. Folder 选择 /(root)
4. 点击 Save

网页地址：https://Gyd792.github.io/upstream-rainfall/

## Actions 权限
仓库 Settings -> Actions -> General -> Workflow permissions，选择 Read and write permissions 并保存，允许定时任务写回数据文件。Actions 也可能需要在 Actions 页面首次手动启用。

## 定时
- `40 23 * * *` UTC = 北京时间次日07:40
- `40 12 * * *` UTC = 北京时间20:40
GitHub schedule 可能延迟，不能保证精确到分钟。

## 费用
保持仓库 Public，使用 GitHub Pages 和标准公共仓库 Actions runner；不启用 Codespaces、付费 Marketplace 或 Larger runners，不绑定付款方式。平台免费政策可能变化，无法保证永久不变。
