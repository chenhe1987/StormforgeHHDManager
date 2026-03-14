import os
import sys
import requests
import json
import subprocess
import re

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
    release_tag = "v1.3.37"
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
            
    # 推送 Tag
    print(f"正在推送 Tag {release_tag}...")
    run_command(f"git tag {release_tag} -m \"Release {release_tag}\"", check=False) # Create if not exists
    tag_result = run_command(f"git push origin {release_tag}", check=False)
    if tag_result:
        print(f"✅ Tag {release_tag} 推送成功！")
    else:
        print(f"⚠️ Tag {release_tag} 推送失败 (可能已存在)。")

    # 5. 创建 Release (需要 Token)
    if not token:
        print("\n[WARN] 跳过 API Release 创建 (无 Token)。")
        print("请手动执行以下操作:")
        print(f"1. 访问 {repo_url}/releases")
        print(f"2. 编辑 Tag {release_tag}")
        print("3. 上传 dist/ 目录下的 zip 文件。")
        
        # 仍然生成 zip 文件方便用户
        dist_zip = f"dist/疾风知硬盘柜管理_{release_tag}.zip"
        if not os.path.exists(dist_zip):
             print(f"正在压缩 {dist_zip}...")
             import shutil
             if not os.path.exists("dist"):
                 os.makedirs("dist")
             # Assuming pyinstaller created dist/疾风知硬盘柜管理_v1.3.27
             src_dir = f"dist/疾风知硬盘柜管理_{release_tag}"
             if os.path.exists(src_dir):
                 shutil.make_archive(dist_zip.replace(".zip", ""), 'zip', src_dir)
                 print(f"✅ ZIP 包已生成: {os.path.abspath(dist_zip)}")
             else:
                 print(f"⚠️ 找不到构建目录: {src_dir}，无法打包。")
        else:
             print(f"✅ ZIP 包已存在: {os.path.abspath(dist_zip)}")
             
        return

    # --- 更新日志配置 (Changelog Configuration) ---
    changelogs = {
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
            dist_zip = f"dist/疾风知硬盘柜管理_{release_tag}.zip"
            # 如果 zip 不存在，尝试先压缩
            if not os.path.exists(dist_zip):
                print(f"正在压缩 {dist_zip}...")
                import shutil
                shutil.make_archive(dist_zip.replace(".zip", ""), 'zip', f"dist/疾风知硬盘柜管理_{release_tag}")
            
            if os.path.exists(dist_zip):
                print(f"正在上传附件: {dist_zip}...")
                # Gitee API 上传附件
                files = {'file': open(dist_zip, 'rb')}
                # 注意：Gitee 上传附件 API 是 POST /repos/{owner}/{repo}/releases/{id}/attach_files
                attach_resp = requests.post(
                    f"https://gitee.com/api/v5/repos/{owner}/{repo}/releases/{release_id}/attach_files",
                    params={"access_token": token},
                    files=files
                )
                if attach_resp.status_code == 201:
                    print("✅ 附件上传成功！")
                else:
                    print(f"⚠️ 附件上传失败: {attach_resp.status_code} - {attach_resp.text}")
            else:
                print(f"⚠️ 找不到附件文件: {dist_zip}")

    print("\n=== 全部完成！ ===")
    print(f"项目地址: {repo_url}")

if __name__ == "__main__":
    main()
