import boto3
from ruamel.yaml import YAML
import os
import subprocess
import urllib.parse
import requests
import sys

PIPELINE_NAME = os.environ['PIPELINE_NAME']
CLUSTER_YML_PATH = os.environ['CLUSTER_YML_PATH']
REGION = os.getenv('AWS_REGION', 'us-east-1')
BRANCH_NAME = f"update-ami-{PIPELINE_NAME}"
BASE_BRANCH = 'main'
GITHUB_TOKEN = os.getenv('PAT_TOKEN')
GITHUB_REPOSITORY = os.getenv('GITHUB_REPOSITORY')


def run_cmd(cmd, check=True):
    print(f"Running command: {' '.join(cmd)}")
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def ensure_branch_exists(branch_name, base_branch='main'):
    run_cmd(['git', 'fetch', 'origin'])
    result = run_cmd(['git', 'ls-remote', '--heads',
                     'origin', branch_name], check=False)
    if not result.stdout.strip():
        print(
            f"Remote branch '{branch_name}' does not exist. Creating from '{base_branch}'...")
        run_cmd(['git', 'checkout', base_branch])
        run_cmd(['git', 'pull', 'origin', base_branch])
        run_cmd(['git', 'checkout', '-b', branch_name])
        repo_url = f"https://x-access-token:{urllib.parse.quote(GITHUB_TOKEN)}@github.com/{GITHUB_REPOSITORY}.git"
        run_cmd(['git', 'push', '-u', repo_url, branch_name])
    else:
        print(f"Remote branch '{branch_name}' exists.")


def branches_differ(branch1, branch2):
    run_cmd(['git', 'fetch', 'origin'])
    # Use origin/branch refs to compare remote tracking branches
    try:
        rev1 = run_cmd(['git', 'rev-parse', branch1]).stdout.strip()
        rev2 = run_cmd(['git', 'rev-parse', branch2]).stdout.strip()
    except subprocess.CalledProcessError:
        print(
            f"One of the branches '{branch1}' or '{branch2}' does not exist locally.")
        return True  # Treat missing branches as different

    if rev1 == rev2:
        print(f"Branches {branch1} and {branch2} are identical.")
        return False

    diff = run_cmd(['git', 'diff', '--quiet',
                   f'{branch1}..{branch2}'], check=False)
    if diff.returncode != 0:
        print(f"Branches {branch1} and {branch2} differ.")
        return True
    else:
        print(f"Branches {branch1} and {branch2} do not differ.")
        return False


def get_latest_available_ami(pipeline_name, region='us-east-1'):
    client = boto3.client('imagebuilder', region_name=region)
    account_id = boto3.client('sts').get_caller_identity()['Account']
    pipeline_arn = f'arn:aws:imagebuilder:{region}:{account_id}:image-pipeline/{pipeline_name}'

    all_images = []
    next_token = None

    while True:
        params = {'imagePipelineArn': pipeline_arn}
        if next_token:
            params['nextToken'] = next_token
        response = client.list_image_pipeline_images(**params)
        all_images.extend(response.get('imageSummaryList', []))
        next_token = response.get('nextToken')
        if not next_token:
            break

    sorted_images = sorted(
        all_images, key=lambda x: x['dateCreated'], reverse=True)

    for image in sorted_images:
        image_arn = image['arn']
        details = client.get_image(imageBuildVersionArn=image_arn)
        if details['image']['state']['status'] == 'AVAILABLE':
            return details['image']['outputResources']['amis'][0]['image']
    return None


def update_yaml_file_preserve_tags(path: str, ami_id: str):
    yaml_parser = YAML()
    yaml_parser.preserve_quotes = True

    with open(path, 'r') as f:
        data = yaml_parser.load(f)

    updated_keys = []

    for key in ['PROD_AMI', 'DEV_AMI']:
        if key in data and data[key] != ami_id:
            data[key] = ami_id
            updated_keys.append(key)

    with open(path, 'w') as f:
        yaml_parser.dump(data, f)

    print(f"✅ Updated {path} with AMI: {ami_id}")
    if updated_keys:
        print("Keys updated:")
        for key in updated_keys:
            print(f"  - {key}")
    else:
        print("ℹ️ No keys needed to be updated.")

    return bool(updated_keys)


def commit_and_push_changes(file_path, ami_id, branch_name):
    run_cmd(['git', 'config', '--global', 'user.name', 'github-actions'])
    run_cmd(['git', 'config', '--global',
            'user.email', 'github-actions@github.com'])

    repo_url = f"https://x-access-token:{urllib.parse.quote(GITHUB_TOKEN)}@github.com/{GITHUB_REPOSITORY}.git"

    # Checkout the branch
    run_cmd(['git', 'checkout', branch_name])
    run_cmd(['git', 'pull', '--ff-only', 'origin', branch_name])

    # Merge main branch into this branch if different
    if branches_differ(f'origin/{BASE_BRANCH}', f'origin/{branch_name}'):
        print(f"Merging '{BASE_BRANCH}' into '{branch_name}'...")
        merge_res = subprocess.run(
            ['git', 'merge', BASE_BRANCH, '--no-edit'], capture_output=True, text=True)
        if merge_res.returncode != 0:
            print(f"Merge conflict or error: {merge_res.stderr}")
            print("❌ Rebase/merge failed. Please resolve conflicts manually.")
            sys.exit(1)
    else:
        print(f"No merge needed; branches are up to date.")

    # Add changes
    run_cmd(['git', 'add', file_path])

    # Check if there are staged changes
    diff_result = subprocess.run(['git', 'diff', '--cached', '--quiet'])
    if diff_result.returncode == 0:
        print("ℹ️ No changes to commit.")
        return False

    # Commit
    run_cmd(['git', 'commit', '-m', f'[NOJIRA]: Update AMI ID to {ami_id}'])

    # Push with --force-with-lease to avoid overwriting others' work
    try:
        run_cmd(['git', 'push', '--force-with-lease', repo_url, branch_name])
    except subprocess.CalledProcessError as e:
        print(f"❌ Push failed: {e}")
        sys.exit(1)

    return True


def create_pull_request(branch_name):
    url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/pulls"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json"
    }
    data = {
        "title": f"[NOJIRA] Update AMI for pipeline {PIPELINE_NAME}",
        "head": branch_name,
        "base": BASE_BRANCH,
        "body": f"This PR updates the AMI ID in `{CLUSTER_YML_PATH}` for the `{PIPELINE_NAME}` pipeline."
    }

    response = requests.post(url, headers=headers, json=data)
    if response.status_code == 201:
        pr_url = response.json()["html_url"]
        print(f"✅ Pull request created: {pr_url}")
    elif response.status_code == 422 and "A pull request already exists" in response.text:
        print(f"ℹ️ Pull request already exists for branch '{branch_name}'.")
    else:
        print(
            f"❌ Failed to create pull request: {response.status_code} {response.text}")


if __name__ == "__main__":
    # Setup: fetch all remote branches
    run_cmd(['git', 'fetch', '--all'])

    # Ensure update branch exists remotely, or create it from main
    ensure_branch_exists(BRANCH_NAME, BASE_BRANCH)

    # Compare branches to check if update needed
    if branches_differ(f'origin/{BASE_BRANCH}', f'origin/{BRANCH_NAME}'):
        print(
            f"Branches differ. Proceeding with AMI update in branch '{BRANCH_NAME}'.")
    else:
        print(f"Branches are identical or update branch is behind. Proceeding with AMI update anyway.")

    ami_id = get_latest_available_ami(PIPELINE_NAME, REGION)
    if not ami_id:
        print("❌ No AVAILABLE AMI found.")
        sys.exit(1)

    updated = update_yaml_file_preserve_tags(CLUSTER_YML_PATH, ami_id)
    if updated:
        committed = commit_and_push_changes(
            CLUSTER_YML_PATH, ami_id, BRANCH_NAME)
        if committed:
            create_pull_request(BRANCH_NAME)
    else:
        print("✅ File already up to date. No changes committed.")
