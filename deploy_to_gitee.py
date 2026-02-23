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
    token = input("请输入您的 Gitee 私人令牌 (Access Token): ").strip()
    if not token:
        print("错误: 必须提供令牌才能继续。")
        sys.exit(1)

    repo_name = "JiFengZhiHDDManager"
    repo_desc = "专为 ASM2074+ASM1153E 芯片方案设计的硬盘柜管理工具，支持智能休眠、安全弹出和 SMART 监控。"
    
    # 2. 调用 Gitee API 创建仓库
    print(f"\n正在创建远程仓库: {repo_name}...")
    headers = {'Content-Type': 'application/json;charset=UTF-8'}
    data = {
        "access_token": token,
        "name": repo_name,
        "description": repo_desc,
        "private": False,  # 公开仓库
        "has_issues": True,
        "has_wiki": True,
        "can_comment": True
    }
    
    response = requests.post("https://gitee.com/api/v5/user/repos", json=data, headers=headers)
    
    repo_url = ""
    git_url = ""

    if response.status_code == 201:
        repo_info = response.json()
        repo_url = repo_info['html_url']
        git_url = repo_info['html_url'] + ".git"
        print(f"✅ 仓库创建成功: {repo_url}")
    elif response.status_code == 400 and "已经存在" in response.text:
        print(f"⚠️ 仓库 {repo_name} 已存在，将使用现有仓库。")
        # 尝试获取用户名来构建 URL
        user_resp = requests.get(f"https://gitee.com/api/v5/user?access_token={token}")
        if user_resp.status_code == 200:
            username = user_resp.json()['login']
            repo_url = f"https://gitee.com/{username}/{repo_name}"
            git_url = f"https://gitee.com/{username}/{repo_name}.git"
        else:
            print("无法获取用户信息，请手动检查。")
            sys.exit(1)
    else:
        print(f"❌ 创建仓库失败: {response.status_code} - {response.text}")
        sys.exit(1)

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
    auth_git_url = git_url.replace("https://", f"https://oauth2:{token}@")
    
    # 尝试使用认证后的 URL 进行推送
    print(f"尝试使用 Token 进行推送...")
    push_result = run_command(f"git push -u {auth_git_url} master", check=False)
    
    if push_result is None:
        print("⚠️ Token 推送失败，尝试使用系统默认 git push...")
        os.system("git push -u origin master")
    else:
        print("✅ 代码推送成功！")
        # 如果使用了 token URL 推送，origin URL 可能会变成带 token 的
        # 为了安全，我们最好把 origin 还原为不带 token 的
        run_command(f"git remote set-url origin {git_url}")

    # 5. 创建 Release
    print("\n正在创建 Release v1.3.27...")
    release_tag = "v1.3.27"
    release_name = "疾风知硬盘柜管理 v1.3.27 (3.5寸硬盘 SMART 修复版)"
    release_body = """
    ## 更新日志
    1. **修复 3.5寸硬盘 SMART 读取问题**: 新增 SAT-12 和 ATA 直通指令尝试机制，解决部分硬盘盒无法读取健康数据的问题。
    2. **新增故障排查工具**: 添加“导出错误日志”按钮，一键打包运行日志和系统信息，方便反馈问题。
    3. **优化**: 延长硬盘响应超时时间，避免因休眠唤醒慢导致的误报。
    
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
