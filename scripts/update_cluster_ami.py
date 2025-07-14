import os
import sys
import subprocess
import urllib.parse
import requests
import yaml

# Read env vars set by GitHub Actions
GITHUB_TOKEN = os.getenv("PAT_TOKEN")
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY")
PIPELINE_NAME = os.getenv("PIPELINE_NAME")
CLUSTER_YML_PATH = os.getenv("CLUSTER_YML_PATH")

if not all([GITHUB_TOKEN, GITHUB_REPOSITORY, PIPELINE_NAME, CLUSTER_YML_PATH]):
    print("❌ One or more required environment variables are missing.")
    sys.exit(1)

BRANCH_NAME = f"update-ami-{PIPELINE_NAME}"


def run_cmd(cmd, capture_output=False):
    print(f"Running command: {' '.join(cmd)}")
    if capture_output:
        result = subprocess.run(
            cmd, check=True, text=True, stdout=subprocess.PIPE)
        return result.stdout.strip()
    else:
        subprocess.run(cmd, check=True)


def remote_branch_exists(branch_name):
    output = run_cmd(
        ["git", "ls-remote", "--heads", "origin", branch_name], capture_output=True
    )
    return bool(output)


def fetch_all():
    run_cmd(["git", "fetch", "--all"])


def checkout_main_branch():
    branches = run_cmd(["git", "branch", "--list", "main"],
                       capture_output=True)
    if "main" not in branches:
        run_cmd(["git", "checkout", "-t", "origin/main"])
    else:
        run_cmd(["git", "checkout", "main"])
    run_cmd(["git", "pull", "origin", "main"])


def create_update_branch_from_main(branch_name):
    print(
        f"Remote branch '{branch_name}' does not exist. Creating from 'main'...")
    checkout_main_branch()
    run_cmd(["git", "checkout", "-b", branch_name])
    repo_url = f"https://x-access-token:{urllib.parse.quote(GITHUB_TOKEN)}@github.com/{GITHUB_REPOSITORY}.git"
    run_cmd(["git", "push", "-u", repo_url, branch_name])


def branches_differ(branch1, branch2):
    fetch_all()
    try:
        commit1 = run_cmd(
            ["git", "rev-parse", f"origin/{branch1}"], capture_output=True)
    except subprocess.CalledProcessError:
        print(f"Branch origin/{branch1} not found.")
        sys.exit(1)
    try:
        commit2 = run_cmd(
            ["git", "rev-parse", f"origin/{branch2}"], capture_output=True)
    except subprocess.CalledProcessError:
        print(f"Branch origin/{branch2} not found.")
        return True
    if commit1 != commit2:
        print(f"Branches origin/{branch1} and origin/{branch2} differ.")
        return True
    print(f"Branches origin/{branch1} and origin/{branch2} are identical.")
    return False


def update_ami_in_yaml(file_path, ami_id):
    with open(file_path) as f:
        data = yaml.safe_load(f)

    updated = False
    keys_updated = []
    if "PROD_AMI" in data and data["PROD_AMI"] != ami_id:
        data["PROD_AMI"] = ami_id
        keys_updated.append("PROD_AMI")
        updated = True
    if "DEV_AMI" in data and data["DEV_AMI"] != ami_id:
        data["DEV_AMI"] = ami_id
        keys_updated.append("DEV_AMI")
        updated = True

    if updated:
        with open(file_path, "w") as f:
            yaml.dump(data, f)
        print(f"✅ Updated {file_path} with AMI: {ami_id}")
        print(f"Keys updated:\n  - " + "\n  - ".join(keys_updated))
    else:
        print(f"ℹ️ No changes needed in {file_path} for AMI {ami_id}")

    return updated


def commit_and_push_changes(file_path, ami_id, branch_name):
    run_cmd(["git", "config", "--global", "user.name", "github-actions"])
    run_cmd(["git", "config", "--global",
            "user.email", "github-actions@github.com"])

    repo_url = f"https://x-access-token:{urllib.parse.quote(GITHUB_TOKEN)}@github.com/{GITHUB_REPOSITORY}.git"

    run_cmd(["git", "checkout", branch_name])

    try:
        run_cmd(["git", "pull", "--rebase", "origin", branch_name])
    except subprocess.CalledProcessError as e:
        print(f"❌ Rebase failed: {e}")
        sys.exit(1)

    run_cmd(["git", "add", file_path])

    diff_result = subprocess.run(["git", "diff", "--cached", "--quiet"])
    if diff_result.returncode == 0:
        print("ℹ️ No changes to commit.")
        return False

    run_cmd(["git", "commit", "-m", f"[NOJIRA]: Update AMI ID to {ami_id}"])

    try:
        run_cmd(["git", "push", "--force-with-lease", repo_url, branch_name])
    except subprocess.CalledProcessError:
        print("❌ Push failed after rebase. Manual intervention required.")
        sys.exit(1)

    return True


def create_pull_request(branch_name):
    url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/pulls"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    params = {"head": branch_name, "base": "main", "state": "open"}
    response = requests.get(url, headers=headers, params=params)
    response.raise_for_status()
    prs = response.json()
    if prs:
        print(f"Pull request already exists: {prs[0]['html_url']}")
        return

    data = {
        "title": f"Update AMI ID for {PIPELINE_NAME}",
        "head": branch_name,
        "base": "main",
        "body": f"This PR updates the AMI ID in `{CLUSTER_YML_PATH}` for the `{PIPELINE_NAME}` pipeline.",
    }
    response = requests.post(url, headers=headers, json=data)
    response.raise_for_status()
    pr = response.json()
    print(f"Pull request created: {pr['html_url']}")


def main():
    # TODO: Replace with real AMI fetch logic or env var
    ami_id = "ami-0a174b9853736c3c8"

    fetch_all()

    if not remote_branch_exists(BRANCH_NAME):
        create_update_branch_from_main(BRANCH_NAME)

    run_cmd(["git", "checkout", BRANCH_NAME])
    run_cmd(["git", "pull", "--ff-only", "origin", BRANCH_NAME])

    if branches_differ("main", BRANCH_NAME):
        print(f"Merging 'main' into '{BRANCH_NAME}' to sync changes...")
        try:
            run_cmd(["git", "merge", "origin/main", "--no-edit"])
            repo_url = f"https://x-access-token:{urllib.parse.quote(GITHUB_TOKEN)}@github.com/{GITHUB_REPOSITORY}.git"
            run_cmd(["git", "push", "--force-with-lease", repo_url, BRANCH_NAME])
        except subprocess.CalledProcessError as e:
            print(f"❌ Merge or push failed: {e}")
            sys.exit(1)
    else:
        print("Branches are identical or update branch is behind. Proceeding with AMI update anyway.")

    updated = update_ami_in_yaml(CLUSTER_YML_PATH, ami_id)
    if not updated:
        print("No update needed; exiting.")
        return

    committed = commit_and_push_changes(CLUSTER_YML_PATH, ami_id, BRANCH_NAME)
    if committed:
        create_pull_request(BRANCH_NAME)
    else:
        print("No new commit was made; skipping PR creation.")


if __name__ == "__main__":
    main()
