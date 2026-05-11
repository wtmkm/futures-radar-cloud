# 期货日内分析工具

一个无需登录的期货日内分析 Web 小工具，打开首页即可查看。支持合约收藏、自动刷新、1/5/10/30 分钟周期、东方财富/新浪行情源和日内操作建议。

## 本地运行

```bash
python app.py
```

默认监听 `0.0.0.0:8765`。也可以用环境变量指定：

```bash
HOST=0.0.0.0 PORT=8765 python app.py
```

## 部署到服务器

推荐用 systemd 托管：

```bash
sudo mkdir -p /opt/futures-intraday
sudo cp -r app.py static /opt/futures-intraday/
sudo cp deploy/futures-intraday.service /etc/systemd/system/futures-intraday.service
sudo systemctl daemon-reload
sudo systemctl enable --now futures-intraday
```

访问：

```text
http://服务器IP:8765
```

如果使用 Nginx，可把域名反代到 `127.0.0.1:8765`。

## 说明

本工具输出为日内辅助研判，不构成投资建议。实盘请结合盘口、成交量、持仓和账户风控。
