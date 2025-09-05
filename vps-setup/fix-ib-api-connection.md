# IB Gateway API连接修复指南

## 问题诊断结果

✅ **SSH端口转发正常**  
✅ **VPS上IB Gateway容器运行正常**  
✅ **本地端口连通性正常**  
❌ **IB Gateway API连接超时**  

## 根本原因

IB Gateway虽然已经启动，但API连接功能可能未正确启用。需要通过VNC连接到IB Gateway界面进行配置。

## 解决步骤

### 1. 通过VNC连接到IB Gateway

```bash
# 确保端口转发正在运行
./vps-setup/quick-start.sh

# 使用VNC客户端连接
# 地址: localhost:5900
# 密码: 检查.env文件中的VNC_SERVER_PASSWORD
```

**推荐VNC客户端:**
- macOS: Screen Sharing (内置) 或 VNC Viewer
- Windows: VNC Viewer
- Linux: Remmina 或 VNC Viewer

### 2. 在IB Gateway中启用API

连接到VNC后，在IB Gateway界面中:

1. **打开API设置**
   - 点击菜单 `Configure` → `API` → `Settings`
   - 或者右键点击系统托盘图标 → `Configure` → `API`

2. **启用API连接**
   - ✅ 勾选 `Enable ActiveX and Socket Clients`
   - ✅ 勾选 `Allow connections from localhost only` (推荐)
   - 设置 `Socket port`: `4002` (Paper Trading) 或 `4001` (Live Trading)
   - 设置 `Master API client ID`: `0` (默认)

3. **应用设置**
   - 点击 `OK` 保存设置
   - IB Gateway会自动重启API服务

### 3. 验证连接

```bash
# 重新测试连接
python diagnose_connection.py
```

## 常见问题解决

### 问题1: VNC连接黑屏
```bash
# 重启IB Gateway容器
ssh vps-ibbot "cd /path/to/docker && docker-compose restart ib-gateway"
```

### 问题2: API设置界面找不到
- 确保IB Gateway完全启动（通常需要1-2分钟）
- 检查是否有弹出的登录对话框需要处理
- 尝试右键点击任务栏中的IB Gateway图标

### 问题3: 设置保存后仍然连接失败
```bash
# 检查IB Gateway日志
ssh vps-ibbot "docker logs ib-gateway --tail 50"

# 重启整个服务
ssh vps-ibbot "cd /path/to/docker && docker-compose restart ib-gateway"
```

### 问题4: 端口冲突
确保以下端口配置正确:
- **容器内部**: 4003 (Live), 4004 (Paper)
- **VPS宿主机**: 4001 (Live), 4002 (Paper)  
- **本地转发**: 4001 (Live), 4002 (Paper)

## 验证步骤

1. **检查端口转发**
   ```bash
   netstat -an | grep -E ':(4001|4002|5900).*LISTEN'
   ```

2. **测试VNC连接**
   ```bash
   nc -z localhost 5900 && echo "VNC可连接"
   ```

3. **测试API连接**
   ```bash
   python diagnose_connection.py
   ```

4. **检查IB Gateway状态**
   ```bash
   ssh vps-ibbot "docker ps | grep ib-gateway"
   ```

## 自动化脚本

运行以下脚本进行完整诊断:
```bash
./vps-setup/diagnose-vps-connection.sh
```

## 注意事项

- IB Gateway启动后需要1-2分钟才能完全就绪
- API设置更改后可能需要重启IB Gateway
- 确保使用正确的交易模式（Live/Paper）对应的端口
- VNC密码可在`.env`文件中查看或修改

## 成功标志

当看到以下输出时，表示连接成功:
```
✓ 端口连通性正常
✓ IBKR API连接成功
✓ 客户端ID X 连接成功
```