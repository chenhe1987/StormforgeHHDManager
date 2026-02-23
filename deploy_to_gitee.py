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
    release_tag = "v1.3.29"
    
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

    print("\n正在创建 Release v1.3.29...")
    release_name = "疾风知硬盘柜管理 v1.3.29 (界面优化)"
    release_body = """
    ## 更新日志
    1. **界面优化**: 修复“导出错误日志”按钮文字不显示的问题，优化按钮样式。
    2. **字体更新**: 全局字体更新为 OPPO Sans (需系统安装，否则回退到 Microsoft YaHei)。
    3. **功能保持**: 包含 v1.3.27 的所有新功能（SMART 修复、错误日志导出）。
    
    ## 包含文件
    - 疾风知硬盘柜管理程序 (exe)
    - 使用指南
    """
    
    release_data = {
        "access_token": token,
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
        
        rel_resp = requests.post(f"https://gitee.com/api/v5/repos/{owner}/{repo}/releases", json=release_data)
        if rel_resp.status_code == 201:
            print(f"✅ Release {release_tag} 创建成功！")
            release_id = rel_resp.json()['id']
            
            # 6. 上传附件
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
                
        elif rel_resp.status_code == 400 and "已存在" in rel_resp.text:
             print(f"⚠️ Release {release_tag} 已存在，跳过创建。")
        else:
            print(f"❌ 创建 Release 失败: {rel_resp.status_code} - {rel_resp.text}")

    print("\n=== 全部完成！ ===")
    print(f"项目地址: {repo_url}")

if __name__ == "__main__":
    main()
