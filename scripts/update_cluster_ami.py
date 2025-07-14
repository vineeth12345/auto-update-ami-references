import os
import subprocess
import sys
from ruamel.yaml import YAML
import requests

# Constants - update these as needed or pass via env
PIPELINE_NAME = os.getenv("PIPELINE_NAME", "amitest")
CLUSTER_YML_PATH = os.getenv("CLUSTER_YML_PATH", "Definitions/clusters.yml")
# Use PAT_TOKEN from GitHub Actions secrets
GITHUB_TOKEN = os.getenv("PAT_TOKEN")
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY")  # e.g., 'user/repo'
REPO_URL = f"https://{GITHUB_TOKEN}@github.com/{GITHUB_REPOSITORY}.git"
BRANCH_NAME = f"update-ami-{PIPELINE_NAME}"

yaml = YAML()
yaml.preserve_quotes = True


def run_cmd(cmd, check=True):
    print(f"Running command: {' '.join(cmd)}")
    result = subprocess.run(cmd, text=True, capture_output=True)
    print(result.stdout)
    if result.stderr.strip():
        print(result.stderr, file=sys.stderr)
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, cmd)
    return result


def remote_branch_exists(branch):
    result = run_cmd(['git', 'ls-remote', '--heads',
                     'origin', branch], check=False)
    return bool(result.stdout.strip())


def branches_differ(branch1, branch2):
    # Get commit hashes of the remote branches
    h1 = run_cmd(['git', 'rev-parse', f'origin/{branch1}']).stdout.strip()
    h2 = run_cmd(['git', 'rev-parse', f'origin/{branch2}']).stdout.strip()
    print(
        f"Branches origin/{branch1} and origin/{branch2} commits: {h1} vs {h2}")
    return h1 != h2


def create_update_branch():
    print(
        f"Remote branch '{BRANCH_NAME}' does not exist. Creating from 'main'...")
    run_cmd(['git', 'checkout', 'main'])
    run_cmd(['git', 'pull', 'origin', 'main'])
    run_cmd(['git', 'checkout', '-b', BRANCH_NAME])
    run_cmd(['git', 'push', '-u', REPO_URL, BRANCH_NAME])


def merge_main_into_update_branch():
    print("Merging 'main' into update branch if needed...")
    run_cmd(['git', 'fetch', 'origin'])
    # Checkout the update branch
    run_cmd(['git', 'checkout', BRANCH_NAME])
    # Pull latest update branch changes
    run_cmd(['git', 'pull', '--ff-only', 'origin', BRANCH_NAME])
    # Check if merge needed
    if branches_differ('main', BRANCH_NAME):
        try:
            run_cmd(['git', 'merge', 'main', '--no-edit'])
        except subprocess.CalledProcessError:
            print("Merge conflict or error. Aborting.")
            run_cmd(['git', 'merge', '--abort'])
            sys.exit(1)
    else:
        print("No merge needed; branches are up to date.")


def update_ami_in_yaml(file_path, ami_id):
    with open(file_path) as f:
        data = yaml.load(f)

    updated = False
    keys_updated = []
    for key in ("PROD_AMI", "DEV_AMI"):
        if key in data and data[key] != ami_id:
            data[key] = ami_id
            keys_updated.append(key)
            updated = True

    if updated:
        with open(file_path, "w") as f:
            yaml.dump(data, f)
        print(f"✅ Updated {file_path} with AMI: {ami_id}")
        print("Keys updated:\n  - " + "\n  - ".join(keys_updated))
    else:
        print(f"ℹ️ No changes needed in {file_path} for AMI {ami_id}")
    return updated


def commit_and_push_changes(ami_id):
    run_cmd(['git', 'config', '--global', 'user.name', 'github-actions'])
    run_cmd(['git', 'config', '--global',
            'user.email', 'github-actions@github.com'])
    run_cmd(['git', 'add', CLUSTER_YML_PATH])

    # Check if there are any changes to commit
    diff_result = run_cmd(['git', 'diff', '--cached', '--quiet'], check=False)
    if diff_result.returncode == 0:
        print("No changes to commit.")
        return False

    commit_msg = f"[NOJIRA]: Update AMI ID to {ami_id}"
    run_cmd(['git', 'commit', '-m', commit_msg])

    # Try pushing changes with --force-with-lease
    try:
        run_cmd(['git', 'push', '--force-with-lease', REPO_URL, BRANCH_NAME])
        print("✅ Push successful.")
    except subprocess.CalledProcessError:
        print("❌ Push failed. Trying pull --rebase and push again...")
        run_cmd(['git', 'pull', '--rebase', 'origin', BRANCH_NAME])
        run_cmd(['git', 'push', '--force-with-lease', REPO_URL, BRANCH_NAME])

    return True


def create_pull_request(branch_name):
    print(f"Creating or updating pull request for branch '{branch_name}'")
    url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/pulls"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json"
    }
    data = {
        "title": f"Update AMI IDs for {PIPELINE_NAME}",
        "head": branch_name,
        "base": "main",
        "body": f"This PR updates the AMI ID in `{CLUSTER_YML_PATH}` for the `{PIPELINE_NAME}` pipeline."
    }

    response = requests.get(
        url + f"?head={GITHUB_REPOSITORY.split('/')[0]}:{branch_name}", headers=headers)
    if response.status_code == 200 and response.json():
        pr_number = response.json()[0]['number']
        print(f"Pull request already exists: #{pr_number}")
        return pr_number
    elif response.status_code != 200:
        print(f"Failed to query PRs: {response.text}")
        return None

    response = requests.post(url, headers=headers, json=data)
    if response.status_code == 201:
        pr_number = response.json()['number']
        print(f"Created pull request #{pr_number}")
        return pr_number
    else:
        print(f"Failed to create PR: {response.text}")
        return None


def main():
    # Fetch all refs
    run_cmd(['git', 'fetch', '--all'])

    # Check if remote update branch exists; if not, create it from main
    if not remote_branch_exists(BRANCH_NAME):
        create_update_branch()

    # Checkout update branch and pull latest changes
    run_cmd(['git', 'checkout', BRANCH_NAME])
    run_cmd(['git', 'pull', '--ff-only', 'origin', BRANCH_NAME])

    # Fetch again and check if branches differ
    run_cmd(['git', 'fetch', 'origin'])
    if branches_differ('main', BRANCH_NAME):
        merge_main_into_update_branch()
    else:
        print("Branches origin/main and origin/update branch are identical or update branch is behind. Proceeding with AMI update anyway.")

    # Here you would get your latest AMI ID, but as an example:
    latest_ami = os.getenv("LATEST_AMI_ID")
    if not latest_ami:
        print("ERROR: Latest AMI ID not provided via LATEST_AMI_ID environment variable")
        sys.exit(1)

    updated = update_ami_in_yaml(CLUSTER_YML_PATH, latest_ami)

    if not updated:
        print("No update needed, exiting.")
        return

    committed = commit_and_push_changes(latest_ami)

    if committed:
        create_pull_request(BRANCH_NAME)


if __name__ == "__main__":
    main()
