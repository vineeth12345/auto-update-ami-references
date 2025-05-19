import boto3
from ruamel.yaml import YAML
from pathlib import Path
import os
import subprocess
import urllib.parse

PIPELINE_NAME = os.environ['PIPELINE_NAME']
CLUSTER_YML_PATH = os.environ['CLUSTER_YML_PATH']
REGION = os.getenv('AWS_REGION', 'us-east-1')
BRANCH_NAME = os.getenv('GITHUB_REF_NAME', 'main')
GITHUB_TOKEN = os.getenv('GITHUB_TOKEN')
GITHUB_REPOSITORY = os.getenv('GITHUB_REPOSITORY')  # e.g., "user/repo"


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
        state = details['image']['state']['status']

        if state == 'AVAILABLE':
            ami_info = details['image']['outputResources']['amis'][0]
            return ami_info['image']

    return None


def update_yaml_file_preserve_tags(path: str, ami_id: str):
    yaml_parser = YAML()
    yaml_parser.preserve_quotes = True

    with open(path, 'r') as f:
        data = yaml_parser.load(f)

    updated_keys = []

    # Top-level keys
    for key in ['PROD_AMI', 'DEV_AMI', 'OVERRIDE_AMI']:
        if key in data and data[key] != ami_id:
            data[key] = ami_id
            updated_keys.append(key)

    # Recursively update nested OVERRIDE_AMI keys
    clusters = data.get('Clusters', {})
    for cluster_name, cluster in clusters.items():
        environments = cluster.get('Environments', {})
        for env_name, env in environments.items():
            if 'OVERRIDE_AMI' in env and env['OVERRIDE_AMI'] != ami_id:
                env['OVERRIDE_AMI'] = ami_id
                updated_keys.append(
                    f'Clusters.{cluster_name}.Environments.{env_name}.OVERRIDE_AMI')

    with open(path, 'w') as f:
        yaml_parser.dump(data, f)

    print(f"✅ Updated {path} with AMI: {ami_id}")
    if updated_keys:
        print("📝 Keys updated:")
        for key in updated_keys:
            print(f"  - {key}")
    else:
        print("ℹ️ No keys needed to be updated.")


def git_commit_and_push(file_path, ami_id, branch_name):
    subprocess.run(['git', 'config', '--global', 'user.name',
                   'github-actions'], check=True)
    subprocess.run(['git', 'config', '--global', 'user.email',
                   'github-actions@github.com'], check=True)

    subprocess.run(['git', 'checkout', branch_name], check=True)
    subprocess.run(['git', 'add', file_path], check=True)

    # Check for changes before committing
    diff_result = subprocess.run(['git', 'diff', '--cached', '--quiet'])
    if diff_result.returncode == 0:
        print("ℹ️ No changes to commit.")
        return

    subprocess.run(
        ['git', 'commit', '-m', f'[NOJIRA]: Update AMI ID to {ami_id}'], check=True)

    encoded_token = urllib.parse.quote(GITHUB_TOKEN)
    repo_url = f"https://x-access-token:{encoded_token}@github.com/{GITHUB_REPOSITORY}.git"
    subprocess.run(['git', 'push', repo_url, branch_name], check=True)


if __name__ == "__main__":
    ami_id = get_latest_available_ami(PIPELINE_NAME, REGION)
    if not ami_id:
        print("❌ No AVAILABLE AMI found.")
        exit(1)

    update_yaml_file_preserve_tags(CLUSTER_YML_PATH, ami_id)
    git_commit_and_push(CLUSTER_YML_PATH, ami_id, BRANCH_NAME)
