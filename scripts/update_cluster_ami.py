import os
import subprocess
import sys
import ruamel.yaml
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

CLUSTER_YML_PATH = os.getenv("CLUSTER_YML_PATH", "Definitions/clusters.yml")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY")
ami_id = os.getenv("AMI_ID")

yaml = ruamel.yaml.YAML()
yaml.preserve_quotes = True


def get_current_branch():
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            check=True,
            stdout=subprocess.PIPE,
            text=True
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        print("❌ Failed to determine current git branch:", e)
        sys.exit(1)


def update_yaml_file_preserve_tags(file_path, new_ami_id):
    with open(file_path, "r") as f:
        data = yaml.load(f)

    updated = False

    for cluster in data.get("clusters", []):
        if "ami" in cluster:
            cluster["ami"] = DoubleQuotedScalarString(new_ami_id)
            updated = True

    if updated:
        with open(file_path, "w") as f:
            yaml.dump(data, f)
        print(f"✅ Updated {file_path} with AMI: {new_ami_id}")
    else:
        print("⚠️ No AMI field found to update.")
        sys.exit(1)


def git_commit_and_push(file_path, new_ami_id, branch_name):
    subprocess.run(['git', 'config', '--global', 'user.name',
                   'github-actions'], check=True)
    subprocess.run(['git', 'config', '--global', 'user.email',
                   'github-actions@github.com'], check=True)

    subprocess.run(['git', 'checkout', branch_name], check=True)
    subprocess.run(['git', 'add', file_path], check=True)

    try:
        subprocess.run(
            ['git', 'commit', '-m', f'[NOJIRA]: Update AMI ID to {new_ami_id}'], check=True)
    except subprocess.CalledProcessError:
        print("ℹ️ No changes to commit.")
        return

    remote_url = f"https://x-access-token:{GITHUB_TOKEN}@github.com/{GITHUB_REPOSITORY}.git"
    subprocess.run(['git', 'remote', 'set-url',
                   'origin', remote_url], check=True)

    subprocess.run(['git', 'push', 'origin', branch_name], check=True)
    print(f"🚀 Changes pushed to {branch_name}")


if __name__ == "__main__":
    if not all([GITHUB_TOKEN, GITHUB_REPOSITORY, ami_id]):
        print("❌ Required environment variables not set.")
        sys.exit(1)

    branch = get_current_branch()
    update_yaml_file_preserve_tags(CLUSTER_YML_PATH, ami_id)
    git_commit_and_push(CLUSTER_YML_PATH, ami_id, branch)
