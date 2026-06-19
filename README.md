# AI硬件周期监控

这是一个在Windows本地运行的监控工具。它每天检查SEC财报和公司官方页面，提取收入、资本开支、库存、应收账款和毛利率，并生成绿黄红仪表盘。

## 第一次运行

在PowerShell中执行：

```powershell
cd "C:\Users\Lenovo\Documents\财经\ai_hardware_monitor"
.\run_monitor.ps1
```

完成后双击 `dashboard.html`，即可在浏览器查看仪表盘。

## 手机应用

项目已经支持PWA。部署到HTTPS网站后，用手机浏览器打开 `index.html`：

- iPhone：点“分享”→“添加到主屏幕”；
- Android：点页面上的“安装到手机”，或浏览器菜单中的“安装应用”。

`.github/workflows/deploy-pages.yml` 会在云端每天北京时间08:15更新数据并部署页面。首次发布仍需创建云端项目并决定公开访问或密码保护。

## 设置每天自动更新

以下命令会创建每天早上8点运行的Windows定时任务：

```powershell
.\install_daily_task.ps1
```

如果希望改为晚上8点：

```powershell
.\install_daily_task.ps1 -Time "20:00"
```

脚本只负责创建任务，不会自动执行。创建后可以在Windows“任务计划程序”中找到 `AI-Hardware-Cycle-Monitor`。

## 手动命令

```powershell
# 更新数据并重新生成仪表盘
.\run_monitor.ps1

# 仅更新数据
& "C:\Users\Lenovo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" .\monitor.py update

# 仅重新生成仪表盘
& "C:\Users\Lenovo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" .\monitor.py dashboard
```

## 数据来源

- SEC EDGAR公司档案和XBRL结构化财务数据；
- 公司投资者关系页面；
- TSMC月度收入页面。

所有SEC文件都在仪表盘中保留官方链接。官方页面监控目前只提示页面发生变化，数字仍以SEC财报为准。

## 当前预警规则

- 红色：收入同比下降；云厂商资本开支同比下降；
- 黄色：库存或应收账款增速超过收入20个百分点；毛利率同比下降超过2个百分点；
- 绿色：上述基础规则未触发；
- 未知：SEC数据不足。

颜色只是筛查工具，不是买卖建议。公司并购、会计口径变化和财年差异都可能造成机械误报，必须打开官方文件复核。

## 文件说明

- `monitor.py`：数据采集、计算和图表生成；
- `config.json`：监控公司及官方页面；
- `data/monitor.db`：SQLite本地数据库；
- `dashboard.html`：可视化仪表盘；
- `data/monitor.log`：每日运行记录。
