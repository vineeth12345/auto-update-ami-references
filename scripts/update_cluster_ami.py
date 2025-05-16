import boto3
import yaml
from pathlib import Path
import os
import subprocess

PIPELINE_NAME = os.environ['PIPELINE_NAME']
CLUSTER_YML_PATH = os.environ['CLUSTER_YML_PATH']
REGION = os.getenv('AWS_REGION', 'us-east-1')


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


def update_yaml_file(path: str, ami_id: str):
    class IgnoreUnknownTagsLoader(yaml.SafeLoader):
        def ignore_unknown(self, node):
            return self.construct_scalar(node)
    IgnoreUnknownTagsLoader.add_constructor(
        None, IgnoreUnknownTagsLoader.ignore_unknown)

    with open(path, 'r') as f:
        data = yaml.load(f, Loader=IgnoreUnknownTagsLoader)

    for key in ['PROD_AMI', 'DEV_AMI', 'OVERRIDE_AMI']:
        if key in data:
            data[key] = ami_id

    with open(path, 'w') as f:
        yaml.dump(data, f, default_flow_style=False)

    print(f"Updated {path} with AMI: {ami_id}")


def git_commit_and_push(file_path, ami_id):
    subprocess.run(['git', 'config', '--global', 'user.name',
                   'github-actions'], check=True)
    subprocess.run(['git', 'config', '--global', 'user.email',
                   'github-actions@github.com'], check=True)

    subprocess.run(['git', 'add', file_path], check=True)
    subprocess.run(
        ['git', 'commit', '-m', f'[NOJIRA]: Update AMI ID to {ami_id}'], check=True)
    subprocess.run(['git', 'push'], check=True)


if __name__ == "__main__":
    ami_id = get_latest_available_ami(PIPELINE_NAME, REGION)
    if not ami_id:
        print("No AVAILABLE AMI found.")
        exit(1)

    update_yaml_file(CLUSTER_YML_PATH, ami_id)
    git_commit_and_push(CLUSTER_YML_PATH, ami_id)
