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
GITHUB_TOKEN = os.getenv('PAT_TOKEN')
GITHUB_REPOSITORY = os.getenv('GITHUB_REPOSITORY')


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


def run_cmd(cmd_list, check=True):
    print(f"Running command: {' '.join(cmd_list)}")
    return subprocess.run(cmd_list, check=check, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def branches_differ(branch1, branch2):
    # Fetch latest from origin
    run_cmd(['git', 'fetch', 'origin'])
    # Check if branches point to same commit
    res = run_cmd(['git', 'rev-parse', branch1])
    rev1 = res.stdout.strip()
    res = run_cmd(['git', 'rev-parse', branch2])
    rev2 = res.stdout.strip()
    if rev1 == rev2:
        print(f"Branches {branch1} and {branch2} are identical.")
        return False

    # Alternatively check diff
    res = run_cmd(['git', 'diff', '--quiet',
                  f'{branch1}..{branch2}'], check=False)
    differ = res.returncode != 0
    if differ:
        print(f"Branches {branch1} and {branch2} differ.")
    else:
        print(f"Branches {branch1} and {branch2} do not differ.")
    return differ


def checkout_branch(branch_name, create_if_missing=False):
    # Check if branch exists locally
    res = run_cmd(['git', 'branch', '--list', branch_name], check=False)
    if res.stdout.strip():
        run_cmd(['git', 'checkout', branch_name])
    else:
        if create_if_missing:
            run_cmd(['git', 'checkout', '-b', branch_name])
        else:
            # Try checkout from remote branch
            run_cmd(['git', 'checkout', '-t', f'origin/{branch_name}'])


def merge_main_into_branch(branch_name):
    # Checkout update branch
    checkout_branch(branch_name)

    # Merge main into it, allow unrelated histories if needed
    try:
        run_cmd(['git', 'merge', 'origin/main',
                '--no-edit', '--allow-unrelated-histories'])
        print(f"✅ Merged origin/main into {branch_name}")
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ Merge failed: {e.stderr}")
        # Abort merge to clean up
        run_cmd(['git', 'merge', '--abort'])
        return False


def commit_and_push_changes(file_path, ami_id, branch_name):
    run_cmd(['git', 'config', '--global', 'user.name', 'github-actions'])
    run_cmd(['git', 'config', '--global',
            'user.email', 'github-actions@github.com'])

    # Checkout update branch
    checkout_branch(branch_name, create_if_missing=True)

    # Check if need to pull/rebase from remote branch if exists
    res = run_cmd(['git', 'ls-remote', '--heads',
                  'origin', branch_name], check=False)
    if res.stdout.strip():
        # Remote branch exists, pull with rebase
        try:
            run_cmd(['git', 'pull', '--rebase', 'origin', branch_name])
        except subprocess.CalledProcessError as e:
            print(f"❌ Rebase failed: {e.stderr}")
            run_cmd(['git', 'rebase', '--abort'])
            sys.exit(1)

    # Add changes
    run_cmd(['git', 'add', file_path])

    # Check if there are staged changes
    diff_result = run_cmd(['git', 'diff', '--cached', '--quiet'], check=False)
    if diff_result.returncode == 0:
        print("ℹ️ No changes to commit.")
        return False

    # Commit
    run_cmd(['git', 'commit', '-m', f'[NOJIRA]: Update AMI ID to {ami_id}'])

    # Push with force-with-lease to avoid overwriting others' work
    encoded_token = urllib.parse.quote(GITHUB_TOKEN)
    repo_url = f"https://x-access-token:{encoded_token}@github.com/{GITHUB_REPOSITORY}.git"

    try:
        run_cmd(['git', 'push', '--force-with-lease', repo_url, branch_name])
        print(f"✅ Pushed changes to branch {branch_name}")
    except subprocess.CalledProcessError as e:
        print(f"❌ Push failed: {e.stderr}")
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
        "base": "main",
        "body": f"This PR updates the AMI ID in `{CLUSTER_YML_PATH}` for the `{PIPELINE_NAME}` pipeline."
    }

    response = requests.post(url, headers=headers, json=data)
    if response.status_code == 201:
        pr_url = response.json()["html_url"]
        print(f"✅ Pull request created: {pr_url}")
    elif response.status_code == 422 and 'A pull request already exists' in response.text:
        print("ℹ️ Pull request already exists.")
    else:
        print(
            f"❌ Failed to create pull request: {response.status_code} {response.text}")


if __name__ == "__main__":
    # Always fetch latest from origin
    run_cmd(['git', 'fetch', '--all'])

    # Checkout main and pull latest
    checkout_branch('main')
    run_cmd(['git', 'pull', 'origin', 'main'])

    # Check if update branch differs from main
    differ = branches_differ('origin/main', f'origin/{BRANCH_NAME}')

    if differ:
        print(
            f"Branches differ. Merging main into {BRANCH_NAME} before updating AMI.")
        merged = merge_main_into_branch(BRANCH_NAME)
        if not merged:
            print("❌ Could not merge main into update branch. Exiting.")
            sys.exit(1)
    else:
        print(f"No differences between main and {BRANCH_NAME}. Proceeding.")

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
        print("✅ File already up to date.")
