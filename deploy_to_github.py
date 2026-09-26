"""GitHub 发布/同步脚本（与 deploy_to_gitee.py 对齐的写法）。

做的事：
  1. 校验 GITHUB_TOKEN（GET /user，确认登录名与 owner 一致）
  2. 仓库不存在则在 GitHub 上创建（公开）
  3. 推送 master + 全部 tag（用带 token 的 URL 推送，**不写入 .git/config**）
  4. 创建/更新该版本的 Release，正文优先取 docs/RELEASE_<版本>.md
  5. 上传 release/Stormforge_DiskManager_<tag>.zip 作为附件（已存在则先删再传）
  6. 核验：远端 refs、Release、附件大小

用法：
  python deploy_to_github.py v1.3.91
  python deploy_to_github.py v1.3.91 --check      # 只校验 token/仓库，不推送
  python deploy_to_github.py v1.3.91 --skip-push  # 只建 Release + 传附件

凭据：环境变量 GITHUB_TOKEN，或项目根目录 .env 里的 GITHUB_TOKEN=...（.env 已被 gitignore）
需要的 token 作用域：classic PAT 勾选 `repo`（创建仓库 + 推送 + Release 附件）
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

import requests

# Windows 控制台默认 GBK，直接打印 ✅/✗ 会 UnicodeEncodeError，先把输出改成 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"
DEFAULT_OWNER = "chenhe1987"
DEFAULT_REPO = "StormforgeHHDManager"
REPO_DESC = ("专为 ASM2074+ASM1153E 芯片方案设计的硬盘柜管理工具，"
             "支持智能休眠、安全弹出和 SMART 监控（Gitee 主站 + GitHub 镜像）。")


def read_token():
    token = (os.environ.get("GITHUB_TOKEN") or "").strip()
    if token:
        return token
    env_file = Path(".env")
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("GITHUB_TOKEN="):
                return line.split("=", 1)[1].strip()
    return ""


def headers(token):
    # GitHub API 要求 User-Agent，缺了会直接 403。
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "JiFengZhiHDDManager-deploy",
    }


def run(command, check=False):
    result = subprocess.run(command, shell=True, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if check and result.returncode:
        print(f"✗ 命令失败: {command}\n{result.stderr.strip()}")
        sys.exit(1)
    return result


def find_zip(tag):
    for candidate in (Path(f"release/Stormforge_DiskManager_{tag}.zip"),
                      Path(f"dist/Stormforge_DiskManager_{tag}.zip"),
                      Path(f"dist/疾风知硬盘柜管理_{tag}.zip")):
        if candidate.exists():
            return candidate
    return None


def release_body(tag):
    note = Path(f"docs/RELEASE_{tag.lstrip('v')}.md")
    if note.exists():
        return note.read_text(encoding="utf-8")
    return f"## {tag}\n\n详见仓库 docs/ 目录与该版本提交记录。"


def ensure_repo(session, token, owner, repo, create=True):
    resp = session.get(f"{API}/repos/{owner}/{repo}", headers=headers(token), timeout=60)
    if resp.status_code == 200:
        print(f"✅ 仓库已存在: {resp.json()['html_url']}")
        return True
    if resp.status_code != 404:
        print(f"⚠️ 查询仓库失败: {resp.status_code} {resp.text[:200]}")
        return False
    if not create:
        print(f"⚠️ 仓库 {owner}/{repo} 不存在（未创建）")
        return False
    payload = {"name": repo, "description": REPO_DESC, "private": False,
               "has_issues": True, "has_wiki": True, "auto_init": False}
    resp = session.post(f"{API}/user/repos", json=payload, headers=headers(token), timeout=60)
    if resp.status_code in (200, 201):
        print(f"✅ 仓库创建成功: {resp.json()['html_url']}")
        return True
    print(f"✗ 创建仓库失败: {resp.status_code} {resp.text[:300]}")
    return False


def ensure_release(session, token, owner, repo, tag):
    name = f"{tag} - 疾风知硬盘柜管理程序"
    body = release_body(tag)
    payload = {"tag_name": tag, "target_commitish": "master", "name": name,
               "body": body, "draft": False, "prerelease": False}
    resp = session.post(f"{API}/repos/{owner}/{repo}/releases",
                        json=payload, headers=headers(token), timeout=60)
    if resp.status_code == 201:
        print(f"✅ Release {tag} 创建成功")
        return resp.json()
    if resp.status_code == 422:
        resp2 = session.get(f"{API}/repos/{owner}/{repo}/releases/tags/{tag}",
                            headers=headers(token), timeout=60)
        if resp2.status_code == 200:
            release = resp2.json()
            print(f"⚠️ Release {tag} 已存在，更新正文…")
            patch = session.patch(f"{API}/repos/{owner}/{repo}/releases/{release['id']}",
                                  json={"name": name, "body": body},
                                  headers=headers(token), timeout=60)
            if patch.status_code == 200:
                print("✅ Release 正文已更新")
                return patch.json()
            print(f"⚠️ 正文更新失败: {patch.status_code}")
            return release
    print(f"✗ 创建 Release 失败: {resp.status_code} {resp.text[:300]}")
    return None


def upload_asset(session, token, owner, repo, release, zip_path):
    url = f"{UPLOADS}/repos/{owner}/{repo}/releases/{release['id']}/assets"
    data = zip_path.read_bytes()
    headers_up = dict(headers(token))
    headers_up["Content-Type"] = "application/zip"
    resp = session.post(url, params={"name": zip_path.name}, data=data,
                        headers=headers_up, timeout=900)
    if resp.status_code == 201:
        print(f"✅ 附件上传成功: {zip_path.name} ({len(data)/1048576:.1f} MB)")
        return resp.json()
    if resp.status_code == 422:      # 同名附件已存在 → 先删再传
        listing = session.get(f"{API}/repos/{owner}/{repo}/releases/{release['id']}/assets",
                              headers=headers(token), timeout=60).json()
        for asset in listing:
            if asset.get("name") == zip_path.name:
                session.delete(f"{API}/repos/{owner}/{repo}/releases/assets/{asset['id']}",
                               headers=headers(token), timeout=60)
                print("⚠️ 已删除同名旧附件，重新上传…")
                resp = session.post(url, params={"name": zip_path.name}, data=data,
                                    headers=headers_up, timeout=900)
                if resp.status_code == 201:
                    print(f"✅ 附件上传成功: {zip_path.name} ({len(data)/1048576:.1f} MB)")
                    return resp.json()
                break
    print(f"✗ 附件上传失败: {resp.status_code} {resp.text[:300]}")
    return None


def main():
    parser = argparse.ArgumentParser(description="GitHub 发布/同步")
    parser.add_argument("tag", help="版本标签，如 v1.3.91")
    parser.add_argument("--owner", default=DEFAULT_OWNER)
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--check", action="store_true", help="只校验 token 与仓库，不改动远端")
    parser.add_argument("--skip-push", action="store_true", help="跳过代码/标签推送")
    parser.add_argument("--skip-release", action="store_true", help="跳过 Release 与附件")
    args = parser.parse_args()

    tag = args.tag if args.tag.startswith("v") else "v" + args.tag
    token = read_token()
    if not token:
        print("✗ 未找到 GITHUB_TOKEN。")
        print("  1) https://github.com/settings/tokens/new  （classic，勾选 repo）")
        print("  2) 在项目根目录 .env 追加一行: GITHUB_TOKEN=你的token（.env 已被 gitignore）")
        return 2

    session = requests.Session()
    me = session.get(f"{API}/user", headers=headers(token), timeout=60)
    if me.status_code != 200:
        print(f"✗ token 无效: {me.status_code} {me.text[:200]}")
        return 2
    login = me.json().get("login")
    print(f"✅ token 有效，登录名: {login}")
    owner = args.owner or login
    if login.lower() != owner.lower():
        print(f"⚠️ owner({owner}) 与 token 所属账号({login}) 不一致，改用 {login}")
        owner = login

    repo_url = f"https://github.com/{owner}/{args.repo}"
    git_url = repo_url + ".git"
    print(f"目标仓库: {repo_url}")

    if args.check:
        exists = session.get(f"{API}/repos/{owner}/{args.repo}",
                             headers=headers(token), timeout=60).status_code == 200
        print("仓库存在:", exists)
        zip_path = find_zip(tag)
        print("发布包:", zip_path or "（未找到，Release 附件会跳过）")
        return 0

    if not ensure_repo(session, token, owner, args.repo):
        return 1

    # remote 只作为便利入口；推送用带 token 的临时 URL，绝不写入 .git/config
    existing = run("git remote get-url github").stdout.strip()
    if existing != git_url:
        run(f'git remote remove github', check=False)
        run(f'git remote add github {git_url}', check=True)
        print(f"✅ 已配置 remote github -> {git_url}")
    else:
        print(f"✅ remote github 已存在 -> {git_url}")

    if not args.skip_push:
        auth_url = f"https://{owner}:{token}@github.com/{owner}/{args.repo}.git"
        print("正在推送 master…")
        res = run(f'git push "{auth_url}" master', check=False)
        print((res.stdout or "").strip() or (res.stderr or "").strip()[-400:])
        print("正在推送全部标签…")
        res = run(f'git push "{auth_url}" --tags', check=False)
        print((res.stdout or "").strip() or (res.stderr or "").strip()[-400:])
        # 确认没把凭据留下
        leak = run("git config --get branch.master.remote").stdout.strip()
        if "token" in leak or owner + ":" in leak:
            run("git config branch.master.remote origin", check=False)
            print("⚠️ 已把 branch.master.remote 还原为 origin")
        else:
            print("✅ .git/config 未残留凭据")

    if args.skip_release:
        return 0

    release = ensure_release(session, token, owner, args.repo, tag)
    if release:
        zip_path = find_zip(tag)
        if zip_path:
            upload_asset(session, token, owner, args.repo, release, zip_path)
        else:
            print(f"⚠️ 未找到 {tag} 的发布包，跳过附件上传")

    # 核验
    print("\n=== 核验 ===")
    ls = run(f"git ls-remote {git_url} master refs/tags/{tag}")
    print((ls.stdout or "").strip() or "（远端未返回 refs）")
    final = session.get(f"{API}/repos/{owner}/{args.repo}/releases/tags/{tag}",
                        headers=headers(token), timeout=60)
    if final.status_code == 200:
        data = final.json()
        print(f"Release: {data['html_url']}  created={data.get('created_at')}")
        for asset in data.get("assets", []):
            print(f"  附件: {asset['name']}  {asset['size']/1048576:.1f} MB  state={asset['state']}")
    else:
        print(f"⚠️ 读取 Release 失败: {final.status_code}")
    print(f"\n项目地址: {repo_url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
