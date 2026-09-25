import os
import sys
import requests
import json
import subprocess
import re
from pathlib import Path


def ensure_release_zip(release_tag):
    """定位/生成发布 ZIP。

    命名沿革：v1.3.77 起构建产物是 Stormforge_DiskManager_<tag>（英文名，dist/release 两处），
    更早是「疾风知硬盘柜管理_<tag>」；这里按优先级查找，找不到再从目录压缩。
    """
    for candidate in (
        Path(f"release/Stormforge_DiskManager_{release_tag}.zip"),
        Path(f"dist/Stormforge_DiskManager_{release_tag}.zip"),
        Path(f"dist/疾风知硬盘柜管理_{release_tag}.zip"),
    ):
        if candidate.exists():
            print(f"✅ ZIP 包已存在: {candidate.resolve()}")
            return candidate

    import shutil
    import zipfile

    for src_dir in (
        Path(f"release/Stormforge_DiskManager_{release_tag}"),
        Path(f"dist/Stormforge_DiskManager_{release_tag}"),
        Path(f"dist/疾风知硬盘柜管理_{release_tag}"),
    ):
        if not src_dir.exists():
            continue
        target = src_dir.parent / f"{src_dir.name}.zip"
        print(f"正在压缩 {target}...")
        # 保留顶层目录（与 zip_release.py 一致），解压即得完整可运行目录。
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zipf:
            for root, _dirs, files in os.walk(src_dir):
                for name in files:
                    path = os.path.join(root, name)
                    zipf.write(path, os.path.relpath(path, src_dir.parent))
        print(f"✅ ZIP 包已生成: {target.resolve()}")
        return target

    print(f"⚠️ 找不到 {release_tag} 的构建目录或 ZIP，跳过附件上传。")
    return None


def upload_release_asset(owner, repo, release_id, token, zip_path):
    print(f"正在上传附件: {zip_path}...")
    with open(zip_path, "rb") as fp:
        attach_resp = requests.post(
            f"https://gitee.com/api/v5/repos/{owner}/{repo}/releases/{release_id}/attach_files",
            params={"access_token": token},
            files={"file": (Path(zip_path).name, fp, "application/zip")},
            timeout=300,
        )

    if attach_resp.status_code == 201:
        print("✅ 附件上传成功！")
        return True

    print(f"⚠️ 附件上传失败: {attach_resp.status_code} - {attach_resp.text}")
    return False

def run_command(command, check=True):
    """运行Shell命令"""
    try:
        result = subprocess.run(command, shell=True, check=check, capture_output=True, text=True, encoding='utf-8')
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        print(f"执行命令失败: {command}")
        print(f"错误信息: {e.stderr}")
        if check:
            sys.exit(1)
        return None

def get_git_config(key):
    """获取git配置"""
    return run_command(f"git config {key}", check=False)

def main():
    print("=== Gitee 自动化部署脚本 ===")
    print("正在初始化...")

    # 1. 获取用户输入
    token = os.environ.get("GITEE_TOKEN", "").strip()
    if not token:
        # Try to read from .env file
        if os.path.exists(".env"):
            with open(".env", "r") as f:
                for line in f:
                    if line.startswith("GITEE_TOKEN="):
                        token = line.split("=", 1)[1].strip()
                        break
    
    if not token:
        print("\n[INFO] 未检测到 GITEE_TOKEN 环境变量或 .env 文件。")
        print("脚本将尝试使用 git 命令直接推送代码 (依赖本地凭证)。")
        print("注意: 如果没有 Token，将跳过 Gitee API 仓库创建和 Release 附件上传步骤。")
        print("      您可能需要手动在 Gitee 网页上创建 Release 并上传 ZIP 包。\n")
        
        choice = input("是否继续? (y/n) [y]: ").strip().lower()
        if choice == 'n':
            token = input("请输入您的 Gitee 私人令牌 (Access Token): ").strip()
    
    repo_name = "JiFengZhiHDDManager"
    repo_desc = "专为 ASM2074+ASM1153E 芯片方案设计的硬盘柜管理工具，支持智能休眠、安全弹出和 SMART 监控。"
    release_tag = "v1.3.59"
    if len(sys.argv) > 1:
        release_tag = sys.argv[1]
        if not release_tag.startswith('v'):
            release_tag = 'v' + release_tag
    
    repo_url = f"https://gitee.com/stormforge/{repo_name}"
    git_url = f"https://gitee.com/stormforge/{repo_name}.git"

    # 2. 调用 Gitee API 创建仓库 (仅当有 token 时)
    if token:
        print(f"\n正在创建/检查远程仓库: {repo_name}...")
        headers = {'Content-Type': 'application/json;charset=UTF-8'}
        data = {
            "access_token": token,
            "name": repo_name,
            "description": repo_desc,
            "private": False,
            "has_issues": True,
            "has_wiki": True,
            "can_comment": True
        }
        
        try:
            response = requests.post("https://gitee.com/api/v5/user/repos", json=data, headers=headers)
            
            if response.status_code == 201:
                repo_info = response.json()
                repo_url = repo_info['html_url']
                git_url = repo_info['html_url'] + ".git"
                print(f"✅ 仓库创建成功: {repo_url}")
            elif response.status_code == 400 and "已经存在" in response.text:
                print(f"⚠️ 仓库 {repo_name} 已存在，将使用现有仓库。")
            else:
                print(f"⚠️ 创建仓库API调用失败: {response.status_code} - {response.text}")
                print("尝试继续使用默认 URL...")
        except Exception as e:
            print(f"⚠️ API 连接失败: {e}")
    else:
        print("跳过仓库创建 (无 Token)，假设仓库已存在...")

    # 3. 更新 README.md 中的链接
    print("\n正在更新 README.md...")
    try:
        with open("README.md", "r", encoding="utf-8") as f:
            content = f.read()
        
        # 替换占位符 YOUR_USERNAME
        # 从 git_url 中提取用户名: https://gitee.com/USERNAME/REPO.git
        match = re.search(r"gitee\.com/([^/]+)/", git_url)
        if match:
            username = match.group(1)
            new_content = content.replace("YOUR_USERNAME", username)
            
            if new_content != content:
                with open("README.md", "w", encoding="utf-8") as f:
                    f.write(new_content)
                print("✅ README.md 已更新。")
                
                # 提交更改
                run_command("git add README.md")
                run_command('git commit -m "Update README with correct Gitee URL"')
            else:
                print("README.md 无需更新。")
    except Exception as e:
        print(f"⚠️ 更新 README 失败: {e}")

    # 4. 配置远程仓库并推送
    print("\n正在配置远程仓库...")
    existing_remote = run_command("git remote get-url origin", check=False)
    if existing_remote:
        if existing_remote != git_url:
            print(f"更新远程仓库 origin: {existing_remote} -> {git_url}")
            run_command(f"git remote set-url origin {git_url}")
    else:
        print(f"添加远程仓库 origin: {git_url}")
        run_command(f"git remote add origin {git_url}")

    print("\n正在推送代码到 master 分支...")
    # 注意：这里可能需要用户输入密码，如果凭证助手未配置
    # 我们可以尝试将 token 嵌入 URL (不推荐，但为了自动化)
    # 格式: https://oauth2:TOKEN@gitee.com/USER/REPO.git
    auth_git_url = git_url
    if token:
        auth_git_url = git_url.replace("https://", f"https://oauth2:{token}@")
        print(f"尝试使用 Token 进行推送...")
    else:
        print("尝试使用系统凭证进行推送...")
    
    # 尝试使用认证后的 URL 进行推送
    push_result = run_command(f"git push -u {auth_git_url} master", check=False)
    
    if push_result is None:
        print("⚠️ 推送失败。")
        if not token:
             print("请检查您是否有权限推送到该仓库，或者尝试手动执行 'git push'。")
    else:
        print("✅ 代码推送成功！")
        # 如果使用了 token URL 推送，origin URL 可能会变成带 token 的
        # 为了安全，我们最好把 origin 还原为不带 token 的
        if token:
            run_command(f"git remote set-url origin {git_url}")
            # 注意：git push -u <带token的URL> 还会把该 URL 写进 branch.master.remote，
            # 不还原的话 token 会明文留在 .git/config 里（v1.3.88 部署时踩到过）。
            run_command("git config branch.master.remote origin")
            run_command("git config branch.master.merge refs/heads/master")
            leaked = run_command("git config --get branch.master.remote", check=False) or ""
            if "oauth2" in leaked or token in leaked:
                print("⚠️ branch.master.remote 仍带着凭据，请手动执行: git config branch.master.remote origin")
            else:
                print("✅ 已确认 .git/config 未残留凭据")
            
    # 推送 Tag（用带凭据的 URL 推，避免 origin 未存凭据时静默失败）
    print(f"正在推送 Tag {release_tag}...")
    run_command(f"git tag {release_tag} -m \"Release {release_tag}\"", check=False) # Create if not exists
    tag_result = run_command(f"git push {auth_git_url} {release_tag}", check=False)
    if tag_result is not None:
        print(f"✅ Tag {release_tag} 推送完成（或已存在）。")
    else:
        print(f"⚠️ Tag {release_tag} 推送失败 (可能已存在)。")

    # 5. 创建 Release (需要 Token)
    if not token:
        print("\n[WARN] 跳过 API Release 创建 (无 Token)。")
        print("请手动执行以下操作:")
        print(f"1. 访问 {repo_url}/releases")
        print(f"2. 编辑 Tag {release_tag}")
        print("3. 上传 dist/ 目录下的 zip 文件。")
        
        ensure_release_zip(release_tag)
             
        return

    # --- 更新日志配置 (Changelog Configuration) ---
    changelogs = {
        "v1.3.88": {
            "name": "v1.3.88 Release - 修复外置盘弹不出去（ReFS 锁卷 error=5）与无分区表盘支持",
            "body": """## v1.3.88 更新日志

### 修复
1. **[修复] 外置盘弹不出去（ReFS/exFAT 卷锁卷失败被当成致命错误）**
   - `FSCTL_LOCK_VOLUME` 对 ReFS/exFAT 卷恒返回 `ERROR_ACCESS_DENIED(5)`，**与是否被占用无关**：
     实测 GUI 完全退出、只剩关机服务时同样返回 5，Restart Manager 查不到占用者，
     本程序自身持有该盘句柄 0 个。
   - v1.3.78～v1.3.87 把 error 5 一律当致命错误，导致 ReFS 盘（本机 E:）永远弹不出去。
     现在对 ReFS/exFAT 跳过锁定、直接 `FSCTL_DISMOUNT_VOLUME`（与 Windows 资源管理器弹出同款），
     **卸载失败仍然立即中止**（不发 SLEEP、不离线）。新增开关
     `eject_dismount_without_lock`（默认 true）。
2. **[修复] 无分区表（RAW）硬盘弹不出去**
   - 旧预检里 `Get-Partition` 对 RAW 盘直接抛异常，整块盘永远弹不出去。
     现在 `PartitionStyle=RAW` 且无卷 → "没有卷需要隔离"，整盘停转 + 弹出
     （事件 `volume_less_disk`）；有分区表却枚举不到卷仍然 fail closed。
3. **[修复] 关机停转静默空转**
   - 没有命中目标时不再只留一行 `总耗时 0.000s`，而是写明原因
     （共享清单为空 / 白名单为空 / 命中的盘都已休眠 / 清单里没有白名单外置盘）。

### 优化
4. **[优化] 弹出失败提示可读化**
   - 不再只回一个 `error=5`：新增 `src/utils/volume_diag.py`，给出
     卷标识 + 文件系统 + 错误码中文解释 + 占用者（Restart Manager）
     + **本程序自身持有该盘句柄数量** + 处理建议；打开物理盘/打开卷/锁定卷分阶段报错。

### 验证
- 本机 ASMT105x 双盘实机验收通过：磁盘 4（GPT + ReFS，1 卷）与磁盘 3（RAW，0 分区）
  均 `ejected_sleep_accepted` + PnP `cr=0 veto=0`，耗时约 4.6s / 4.4s。
- 新增 `tools/test_eject_diagnostics.py`（21 项，含真 `WindowsBackend` + IOCTL 打桩的
  完整事务测试）；新增手动诊断工具 `tools/volume_occupancy_probe.py`。
- 技术原则固化：`docs/TECH_NOTES_弹出休眠机制.md` §8.41 + 修改红线第 10–15 条。"""
        },
        "v1.3.59": {
            "name": "v1.3.59 Release - 修复关机前硬盘被再次唤醒",
            "body": """## v1.3.59 更新日志

### 修复
1. **[修复] 关机前硬盘被再次唤醒**
   - 修复了部分环境下，硬盘在收到关机休眠指令后又被程序后台监控重新访问并唤醒的问题。
2. **[优化] 关机静默模式**
   - 新增关机静默模式。收到 `WM_QUERYENDSESSION` 后，后台 SMART 检测和磁盘重新枚举会立即停止，减少程序自身对硬盘的最后时刻访问。
3. **[优化] 关机流程仅使用缓存**
   - 关机阶段严格只使用现有磁盘缓存，不再回退到在线扫描，避免为了补全设备列表而重新唤醒外置机械盘。"""
        },
        "v1.3.58": {
            "name": "v1.3.58 Release - 修复安全弹出前的自占用问题",
            "body": """## v1.3.58 更新日志

### 修复
1. **[修复] 程序自身导致的锁卷失败**
   - 在安全弹出前主动释放 WMI / COM 查询对象，缓解程序自身持有卷句柄导致的 `error=5` 锁卷失败和弹出 veto 问题。
2. **[优化] 卷预处理超时**
   - 适度延长卷预处理超时时间，提高部分外置硬盘在短时间内完成锁卷和卸载的成功率。"""
        },
        "v1.3.45": {
            "name": "v1.3.45 Release - 修复升级后自启动失效并强化休眠保护",
            "body": """## v1.3.45 更新日志

### 修复
1. **[修复] 升级后自启动失效**
   - 修复了版本升级后“随系统启动”可能仍指向旧版本 EXE 的问题。程序现在会自动检测并修复过期的注册表启动项路径。
2. **[优化] 手动休眠后的后台保护**
   - 优化了后台监控逻辑。只要用户手动让硬盘进入休眠，程序就不再自动重新枚举该硬盘，直到用户手动唤醒或点击刷新，从而避免程序自身再次唤醒硬盘。"""
        },
        "v1.3.44": {
            "name": "v1.3.44 Release - 修复设备刷新卡顿与幽灵设备报错",
            "body": """## v1.3.44 更新日志

### 修复
1. **[修复] 设备刷新卡顿问题**
   - 修复了用户在手动点击“刷新设备列表”时，程序错误地去扫描并尝试恢复所有曾经连接过但当前已拔出的 USB 设备（幽灵设备/Phantom Device），导致程序长时间卡顿（可能长达 30 秒）并输出大量 `CM_PROB_PHANTOM` 错误日志的问题。现在刷新设备将瞬间完成，且只处理真正存在的异常设备。"""
        },
        "v1.3.43": {
            "name": "v1.3.43 Release - 强化关机保护与设置记忆",
            "body": """## v1.3.43 更新日志

### 功能增强
1. **[增强] 关机保护优先级**
   - 提高了程序在系统关机时的优先级，确保在系统强制终止进程前有足够时间完成硬盘弹出。
2. **[优化] 关机信号处理**
   - 同时支持 `WM_QUERYENDSESSION` 和 `WM_ENDSESSION` 信号，提升了在不同 Windows 版本下的关机保护可靠性。
3. **[修复] 设置记忆问题**
   - 修复了配置管理器（ConfigManager）在多线程环境下的同步问题，确保“关机自动弹出”等选项能被正确保存和加载。
4. **[优化] 单例模式**
   - 重构配置管理器为单例模式，彻底解决多实例导致的配置覆盖冲突。"""
        },
        "v1.3.36": {
            "name": "v1.3.36 Release - 修复浏览器跳转与交互优化",
            "body": """## v1.3.36 更新日志

### 功能修复
1. **[修复] 浏览器跳转问题**
   - 修复了在某些系统环境下（尤其是管理员权限运行时）点击“检查软件更新”无法自动打开浏览器的问题。
   - 引入了 `os.startfile` 作为备选方案，提升了跳转成功率。
2. **[优化] 版本检查交互**
   - 即使当前已是最新版本，现在也提供前往项目主页的快捷链接。"""
        },
        "v1.3.35": {
            "name": "v1.3.35 Release - 修复通电时间显示异常",
            "body": """## v1.3.35 更新日志

### 重要修复
1. **[修复] SMART 通电时间解析错误**
   - 修复了部分硬盘（如西数）在读取 SMART 属性 9 时，因厂商自定义高位数据导致的“使用时间显示为 169 亿年”的严重 Bug。
   - 增加了原始值合理性校验，自动屏蔽非标准数据。"""
        },
        "v1.3.34": {
            "name": "v1.3.34 Release - 界面布局优化与接口修正",
            "body": """## v1.3.34 更新日志

### 界面优化
1. **[布局] 功能按钮位置调整**
   - 将“刷新设备列表”、“检查软件更新”、“导出错误日志”、“随系统启动”移动至左侧侧边栏底部，解决了硬盘列表过多时遮挡按钮的问题。
2. **[修正] 接口类型显示**
   - 修正了 USB 硬盘接口显示，由 "IDE" 改为更准确的 "USB (SATA)"。"""
        }
    }

    # 获取当前版本的日志
    log_info = changelogs.get(release_tag, {
        "name": f"{release_tag} Release",
        "body": f"## {release_tag} 更新日志\n\n暂无详细更新日志。"
    })

    print(f"\n正在更新 Release {release_tag}...")
    release_name = log_info["name"]
    release_body = log_info["body"]
    
    release_data = {
        "tag_name": release_tag,
        "name": release_name,
        "body": release_body,
        "prerelease": False,
        "target_commitish": "master"
    }
    
    # 获取 owner 和 repo
    match = re.search(r"gitee\.com/([^/]+)/([^/]+)(\.git)?", git_url)
    if match:
        owner = match.group(1)
        repo = match.group(2).replace(".git", "")
        
        # 检查 Tag 是否存在
        # Gitee API 创建 Release 会自动创建 Tag
        
        rel_resp = requests.post(f"https://gitee.com/api/v5/repos/{owner}/{repo}/releases", json=release_data, params={"access_token": token})
        if rel_resp.status_code == 201:
            print(f"✅ Release {release_tag} 创建成功！")
            release_id = rel_resp.json()['id']
        elif rel_resp.status_code == 400:
             print(f"⚠️ Release {release_tag} 可能已存在 (400)，尝试获取 ID...")
             # 获取已存在的 Release ID
             get_rel_resp = requests.get(f"https://gitee.com/api/v5/repos/{owner}/{repo}/releases/tags/{release_tag}", params={"access_token": token})
             if get_rel_resp.status_code == 200:
                 release_id = get_rel_resp.json()['id']
                 print(f"✅ 获取到 Release ID: {release_id}")
                 
                 # 更新已存在的 Release 内容
                 print(f"正在更新已存在的 Release {release_tag} 内容...")
                 patch_data = {
                     "tag_name": release_tag,
                     "name": release_name,
                     "body": release_body
                 }
                 patch_resp = requests.patch(f"https://gitee.com/api/v5/repos/{owner}/{repo}/releases/{release_id}", json=patch_data, params={"access_token": token})
                 if patch_resp.status_code == 200:
                     print(f"✅ Release {release_tag} 内容更新成功！")
                 else:
                     print(f"⚠️ Release 内容更新失败: {patch_resp.status_code}")
             else:
                 print(f"❌ 无法获取 Release ID: {get_rel_resp.status_code}")
                 return
        else:
            print(f"❌ 创建 Release 失败: {rel_resp.status_code} - {rel_resp.text}")
            return

        # 6. 上传附件 (无论新建还是已存在，都尝试上传)
        if release_id:
            dist_zip = ensure_release_zip(release_tag)
            if dist_zip:
                upload_release_asset(owner, repo, release_id, token, dist_zip)

    print("\n=== 全部完成！ ===")
    print(f"项目地址: {repo_url}")

if __name__ == "__main__":
    main()
