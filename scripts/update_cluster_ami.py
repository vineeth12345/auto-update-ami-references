import boto3
from ruamel.yaml import YAML
import os
import subprocess
import urllib.parse
import requests

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


def git_create_branch_and_commit(file_path, ami_id, branch_name):
    subprocess.run(['git', 'config', '--global', 'user.name',
                   'github-actions'], check=True)
    subprocess.run(['git', 'config', '--global', 'user.email',
                   'github-actions@github.com'], check=True)

    subprocess.run(['git', 'fetch'], check=True)

    result = subprocess.run(
        ['git', 'ls-remote', '--heads', 'origin', branch_name],
        stdout=subprocess.PIPE,
        text=True
    )

    if result.stdout:
        print(f"🔁 Branch '{branch_name}' exists remotely. Rebasing...")
        subprocess.run(['git', 'checkout', branch_name], check=True)
        subprocess.run(['git', 'pull', '--rebase',
                       'origin', branch_name], check=True)
    else:
        print(f"🌱 Creating new branch '{branch_name}'...")
        subprocess.run(['git', 'checkout', '-b', branch_name], check=True)

    if not ami_id:
        return True  # Skip committing if only setting up the branch

    subprocess.run(['git', 'add', file_path], check=True)

    diff_result = subprocess.run(['git', 'diff', '--cached', '--quiet'])
    if diff_result.returncode == 0:
        print("ℹ️ No changes to commit.")
        return False

    subprocess.run(
        ['git', 'commit', '-m', f'[NOJIRA]: Update AMI ID to {ami_id}'], check=True)

    encoded_token = urllib.parse.quote(GITHUB_TOKEN)
    repo_url = f"https://x-access-token:{encoded_token}@github.com/{GITHUB_REPOSITORY}.git"
    subprocess.run(['git', 'push', '--force-with-lease',
                   repo_url, branch_name], check=True)

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
    else:
        print(
            f"❌ Failed to create pull request: {response.status_code} {response.text}")


if __name__ == "__main__":
    ami_id = get_latest_available_ami(PIPELINE_NAME, REGION)
    if not ami_id:
        print("❌ No AVAILABLE AMI found.")
        exit(1)

    # Step 1: Setup branch before modifying the file
    git_create_branch_and_commit(CLUSTER_YML_PATH, None, BRANCH_NAME)

    # Step 2: Modify the file
    updated = update_yaml_file_preserve_tags(CLUSTER_YML_PATH, ami_id)

    # Step 3: Commit and push if updated
    if updated:
        committed = git_create_branch_and_commit(
            CLUSTER_YML_PATH, ami_id, BRANCH_NAME)
        if committed:
            create_pull_request(BRANCH_NAME)
    else:
        print("✅ File already up to date.")
