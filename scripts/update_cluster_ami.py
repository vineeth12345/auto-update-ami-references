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


def run_cmd(cmd, check=True, capture_output=False, text=True):
    print(f"Running command: {' '.join(cmd)}")
    return subprocess.run(cmd, check=check, capture_output=capture_output, text=text)


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


def setup_branch(branch_name):
    run_cmd(['git', 'config', '--global', 'user.name', 'github-actions'])
    run_cmd(['git', 'config', '--global',
            'user.email', 'github-actions@github.com'])
    run_cmd(['git', 'fetch', '--all'])

    # Check if remote branch exists
    result = run_cmd(['git', 'ls-remote', '--heads', 'origin',
                     branch_name], capture_output=True)
    if result.stdout.strip():
        print(
            f"🔁 Branch '{branch_name}' exists remotely. Checking out and rebasing...")
        run_cmd(['git', 'checkout', branch_name])
        run_cmd(['git', 'pull', '--rebase', 'origin', branch_name])
    else:
        print(
            f"🌱 Remote branch '{branch_name}' does not exist. Creating from main...")
        run_cmd(['git', 'checkout', 'main'])
        run_cmd(['git', 'pull', 'origin', 'main'])
        run_cmd(['git', 'checkout', '-b', branch_name])
        run_cmd(['git', 'push', '-u',
                f'https://x-access-token:{urllib.parse.quote(GITHUB_TOKEN)}@github.com/{GITHUB_REPOSITORY}.git', branch_name])


def branches_differ(branch_a='origin/main', branch_b=None):
    if branch_b is None:
        branch_b = BRANCH_NAME
    run_cmd(['git', 'fetch', 'origin'])
    rev_a = run_cmd(['git', 'rev-parse', branch_a],
                    capture_output=True).stdout.strip()
    rev_b = run_cmd(['git', 'rev-parse', branch_b],
                    capture_output=True).stdout.strip()
    print(f"Branches {branch_a} and {branch_b} commits: {rev_a} vs {rev_b}")
    return rev_a != rev_b


def merge_main_into_branch(branch_name):
    print(f"🔀 Merging 'main' into '{branch_name}'...")
    run_cmd(['git', 'checkout', branch_name])
    try:
        run_cmd(['git', 'merge', 'main', '--no-edit'])
        print("✅ Merge successful.")
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ Merge failed: {e}. Please resolve conflicts manually.")
        sys.exit(1)


def commit_and_push_changes(file_path, ami_id, branch_name):
    run_cmd(['git', 'add', file_path])

    diff_result = subprocess.run(['git', 'diff', '--cached', '--quiet'])
    if diff_result.returncode == 0:
        print("ℹ️ No changes to commit.")
        return False

    run_cmd(['git', 'commit', '-m', f'[NOJIRA]: Update AMI ID to {ami_id}'])

    encoded_token = urllib.parse.quote(GITHUB_TOKEN)
    repo_url = f"https://x-access-token:{encoded_token}@github.com/{GITHUB_REPOSITORY}.git"

    # Push with --force-with-lease to avoid stale refs
    try:
        run_cmd(['git', 'push', '--force-with-lease', repo_url, branch_name])
        print("✅ Push successful.")
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ Push failed: {e}")
        # Attempt to pull rebase and push again once
        run_cmd(['git', 'pull', '--rebase', 'origin', branch_name])
        run_cmd(['git', 'push', '--force-with-lease', repo_url, branch_name])
        print("✅ Push successful after rebase.")
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
    elif response.status_code == 422 and "A pull request already exists" in response.text:
        print("ℹ️ Pull request already exists.")
    else:
        print(
            f"❌ Failed to create pull request: {response.status_code} {response.text}")


if __name__ == "__main__":
    ami_id = get_latest_available_ami(PIPELINE_NAME, REGION)
    if not ami_id:
        print("❌ No AVAILABLE AMI found.")
        sys.exit(1)

    setup_branch(BRANCH_NAME)

    # Check if main and update branch differ
    if branches_differ('origin/main', f'origin/{BRANCH_NAME}'):
        merge_main_into_branch(BRANCH_NAME)
    else:
        print("Branches are identical or update branch is behind. Proceeding with AMI update anyway.")

    updated = update_yaml_file_preserve_tags(CLUSTER_YML_PATH, ami_id)

    if updated:
        committed = commit_and_push_changes(
            CLUSTER_YML_PATH, ami_id, BRANCH_NAME)
        if committed:
            create_pull_request(BRANCH_NAME)
    else:
        print("✅ File already up to date.")
