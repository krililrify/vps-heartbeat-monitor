# 升级到 2.0（手动部署版）

2.0 新增：厂商（带历史）、IP/密码点击复制、加密保存 root 密码、⚙ 管理按钮、网页 SSH 终端。
**探针没有改动，已安装的探针不用重装。** 数据库会在启动时自动迁移，旧数据保留。

## 1. 生成 SECRET_KEY 并写入配置（必须，只做一次）

```bash
echo "SECRET_KEY=$(openssl rand -hex 32)" >> /etc/hbm.env
grep SECRET_KEY /etc/hbm.env      # 确认已写入；请把这个值另存一份
```

没有 SECRET_KEY，服务会拒绝启动。之后不要更换它，否则已保存的密码无法解密。

## 2. 替换代码并升级

把 2.0 的文件覆盖到你的仓库，提交推送：

```bash
git add . && git commit -m "feat: v2.0 vendor, saved password, copy, gear, web terminal" && git push
```

在控制端服务器上：

```bash
/opt/update-hbm.sh        # 自动备份数据库 → 拉取 → 安装依赖(含 xterm) → 构建前端 → 重启
systemctl status hbm --no-pager
journalctl -u hbm -n 30 --no-pager
```

## 3. 验证

1. 刷新网页（Ctrl+F5），列表出现 ⚙、厂商、root 密码、终端 列。
2. 旧节点「未保存」：点 ⚙，补填厂商和 root 密码，保存。
3. 点 IP、密码，应提示「已复制」。
4. 点「终端」，新标签页应显示 root 的命令行；关闭标签页即断开。

## 常见问题

| 现象 | 处理 |
|---|---|
| 服务起不来，日志提示 SECRET_KEY | 按第 1 步写入 `/etc/hbm.env` 后 `systemctl restart hbm` |
| 终端标签页提示「连接失败：请确认已登录」 | 先在原页面登录；确认用的是和 `PUBLIC_URL` 完全相同的域名访问 |
| 终端一直「正在连接」 | 控制端到目标 VPS 的 SSH 端口是否通；看 `journalctl -u hbm -n 50` |
| 提示「已保存的密码无法解密」 | SECRET_KEY 被更换过；换回原值，或在 ⚙ 里重新填写密码 |
| 终端提示指纹不一致 | VPS 重装过系统；确认安全后，删除该节点重新添加 |
| 复制没反应 | 必须通过 HTTPS 访问；浏览器需允许剪贴板 |

## 回滚

`git revert` 后重新执行 `/opt/update-hbm.sh`。2.0 只给数据库**新增**了字段和表，1.0 代码可以直接读同一个数据库。
