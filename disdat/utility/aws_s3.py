#
# Copyright 2015, 2016, 2017 Human Longevity, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
"""
Utilities for accessing AWS using boto3

@author: twong / kyocum
@copyright: Human Longevity, Inc. 2017
@license: Apache 2.0
"""

# We use boto3_session_cache as a drop-in replacement for boto3 so that
# users that use AWS profiles with MFA tokens aren't constantly asked to
# enter new token values.
import base64
import boto3_session_cache as b3
import disdat.common as common
import logging
import os
import pkg_resources

from botocore.exceptions import ClientError
from urlparse import urlparse

_logger = logging.getLogger(__name__)


def batch_get_job_definition_name(pipeline_class_name):
    """Get the most recent active AWS Batch job definition for a dockerized
    pipeline.
    """
    return '{}-job-definition'.format(common.make_pipeline_image_name(pipeline_class_name))


def batch_get_job_definition(job_definition_name):
    """Get the most recent active revision number for a AWS Batch job
    definition

    Args:
        job_definition_name: The name of the job definition

    Return:
        The fully-qualified job definition name with revision number, or
            `None` if the job definition does not exist
    """
    region = profile_get_region()
    client = b3.client('batch', region_name=region)
    response = client.describe_job_definitions(jobDefinitionName=job_definition_name, status='ACTIVE')
    if response['ResponseMetadata']['HTTPStatusCode'] != 200:
        raise RuntimeError(
            'Failed to get job definition revisions for {}: HTTP Status {}'.format(job_definition_name, response['ResponseMetadata']['HTTPStatusCode'])
        )
    job_definitions = response['jobDefinitions']
    revision = 0
    for j in job_definitions:
        if j['jobDefinitionName'] != job_definition_name:
            continue
        if j['revision'] > revision:
            revision = j['revision']
    if revision == 0:
        return None
    else:
        return '{}:{}'.format(job_definition_name, revision)


def batch_register_job_definition(job_definition_name, remote_pipeline_image_name, vcpus=1, memory=2000):
    """Register a new AWS Batch job definition.

    Args:
        job_definition_name: The name of the job definition
        remote_pipeline_image_name: The ECR Docker image to load to run jobs
            using this definition
        vcpus: The number of vCPUs to use to run jobs using this definition
        memory: The amount of memory in MiB to use to run jobs using this
            definition
    """
    region = profile_get_region()
    client = b3.client('batch', region_name=region)
    response = client.register_job_definition(
        jobDefinitionName=job_definition_name,
        type='container',
        containerProperties={
            'image': remote_pipeline_image_name,
            'vcpus': vcpus,
            'memory': memory,
        }
    )
    if response['ResponseMetadata']['HTTPStatusCode'] != 200:
        raise RuntimeError('Failed to create job definition {}: HTTP Status {}'.format(job_definition_name, response['ResponseMetadata']['HTTPStatusCode']))


def ecr_create_fq_respository_name(repository_name, policy_resource_package=None, policy_resource_name=None):
    ecr_client = b3.client('ecr', region_name=profile_get_region())
    # Create or fetch the repository in AWS (to store the image)
    try:
        response = ecr_client.create_repository(
            repositoryName=repository_name
        )
        repository_metadata = response['repository']
        # Set the policy on the repository
        if policy_resource_package is not None and policy_resource_name is not None:
            policy = pkg_resources.resource_string(policy_resource_package.__name__, policy_resource_name)
            _ = ecr_client.set_repository_policy(
                registryId=repository_metadata['registryId'],
                repositoryName=repository_name,
                policyText=policy,
                force=True
            )
    except ClientError as e:
        if e.response['Error']['Code'] == 'RepositoryAlreadyExistsException':
            response = ecr_client.describe_repositories(
                repositoryNames=[repository_name]
            )
            repository_metadata = response['repositories'][0]
        else:
            raise e
    return repository_metadata['repositoryUri']


def ecr_get_fq_respository_name(repository_name):
    return ecr_create_fq_respository_name(repository_name)


def ecr_get_auth_config():
    ecr_client = b3.client('ecr', region_name=profile_get_region())
    # Authorize docker to push to ECR
    response = ecr_client.get_authorization_token()
    if response['ResponseMetadata']['HTTPStatusCode'] != 200:
        raise RuntimeError('Failed to get AWS ECR authorization token: HTTP Status {}'.format(response['ResponseMetadata']['HTTPStatusCode']))
    token = response['authorizationData'][0]['authorizationToken']
    username, password = base64.decodestring(token).split(':')
    return {'username': username, 'password': password}


def profile_get_region():
    """Get the AWS region for the current AWS profile.
    """
    def _get_region(profiles, profile_name):
        if profile_name not in profiles:
            raise KeyError('AWS profile {} not defined in AWS config'.format(profile_name))
        try:
            profile = profiles[profile_name]
            return profile['region']
        except KeyError:
            try:
                profile_name = profile['source_profile']
                return _get_region(profiles, profile_name)
            except KeyError:
                return None
    session = b3.session()
    if 'AWS_PROFILE' in os.environ:
        profile_name = os.environ['AWS_PROFILE']
    else:
        profile_name = 'default'
    profiles = session.full_config['profiles']
    region = _get_region(profiles, profile_name)
    if 'AWS_DEFAULT_REGION' in os.environ:
        region = os.environ['AWS_DEFAULT_REGION']
    return region


def s3_path_exists(s3_url):
    """
    Given an entire path, does the key exist?

    If you're checking for partial key, make sure to end with '/'

    This is how you make "folders" in s3, you use a key ending with '/'
    e.g., s3://mybucket/onelevel/anotherdir/
    bucket = mybucket
    key = onelevel/anotherdir/ -- it's a zero size object.

    If checking for full path, you can end with thing itself.

    Args:
        s3_url:

    Returns:

    """
    import botocore

    s3 = b3.resource('s3')
    bucket, key = split_s3_url(s3_url)
    if key is None:
        return s3_bucket_exists(bucket)
    exists = True
    try:
        s3.Object(bucket, key).load()
    except botocore.exceptions.ClientError as e:
        error_code = int(e.response['Error']['Code'])
        _logger.info("Error code {}".format(error_code))
        if error_code == 404:
            exists = False
        else:
            raise

    return exists


def s3_bucket_exists(bucket):
    """
    Code from Amazon docs for checking bucket existence.

    Args:
        bucket:

    Returns:
        booL: whether bucket exists

    """
    import botocore

    s3 = b3.resource('s3')
    exists = True
    try:
        s3.meta.client.head_bucket(Bucket=bucket)
    except botocore.exceptions.ClientError as e:
        error_code = int(e.response['Error']['Code'])
        if error_code == 404:
            exists = False
        elif error_code == 403:
            # for buckets you can get a forbidden instead of resource not found
            # if you have the s3:ListBucket permission on the bucket, Amazon S3 will return a
            # HTTP status code 404 ("no such key") error. If you don't have the s3:ListBucket permission,
            # Amazon S3 will return a HTTP status code 403 ("access denied") error.
            _logger.info("aws_s3: bucket {} raised a 403 (access forbidden), do you have ListBucket permission?".format(bucket))
            exists = False
        else:
            raise
    return exists


def ls_s3_url_objects(s3_url):
    """
    Return aws boto3 ObjectSummary's

    Note: There is no current way in boto3 to do globs -- you filter on the client side.

    Returns:
        list:str: list of ObjectSummary's under this path
    """
    result = []

    if s3_url[-1] is not '/':
        s3_url += '/'

    s3 = b3.resource('s3')
    bucket, s3_path = split_s3_url(s3_url)

    if not s3_bucket_exists(bucket):
        return result

    s3_b = s3.Bucket(bucket)
    for i in s3_b.objects.filter(Prefix=s3_path, MaxKeys=1024):
        result.append(i)
    if len(result) == 1024:
        _logger.warn("ls_s3_url_objects: hit MaxKeys 1024 limit in result set.")

    return result


def ls_s3_url(s3_url):
    """
    Return path strings at this url

    Returns:
        (bool) : removed
    """

    return [os.path.join('s3://', obj.bucket_name, obj.key) for obj in ls_s3_url_objects(s3_url)]


def delete_s3_dir(s3_url):
    s3 = b3.resource('s3')
    bucket, s3_path = split_s3_url(s3_url)
    bucket = s3.Bucket(bucket)
    objects_to_delete = []
    for obj in bucket.objects.filter(Prefix=s3_path):
        objects_to_delete.append({'Key': obj.key})
    bucket.delete_objects(
        Delete={
            'Objects': objects_to_delete
        }
    )


def delete_s3_file(s3_url):
    s3 = b3.resource('s3')
    bucket, s3_path = split_s3_url(s3_url)
    response = s3.Object(bucket, s3_path).delete()
    # print response
    # if response['DeleteMarker']:
    #    return True
    # else:
    #    return False
    # TODO: we're getting a different response than the docs say.
    return True


def cp_s3_file(s3_src_path, s3_root):
    """
    Copy an s3 file to an s3 location
    Keeps the original file name.
    Args:
        s3_src_path:
        s3_root:

    Returns:

    """
    s3 = b3.resource('s3')
    bucket, s3_path = split_s3_url(s3_root)
    filename = os.path.basename(s3_src_path)
    output_path = os.path.join(s3_path, filename)

    src_bucket, src_key = split_s3_url(s3_src_path)
    # print "Trying to copy from bucket {} key {} to bucket {} key {}".format(src_bucket, src_key, bucket, output_path)

    s3.Object(bucket, output_path).copy_from(CopySource={'Bucket': src_bucket, 'Key': src_key}, ServerSideEncryption="AES256")
    return os.path.join("s3://", bucket, output_path)


def put_s3_file(local_path, s3_root):
    """
    Put local file to location at s3_root.
    Keeps original file name.
    Args:
        local_path:
        s3_root:

    Returns:

    """
    s3 = b3.resource('s3')
    bucket, s3_path = split_s3_url(s3_root)
    filename = os.path.basename(local_path)
    s3.Object(bucket, os.path.join(s3_path, filename)).upload_file(local_path, ExtraArgs={"ServerSideEncryption": "AES256"})
    return filename


def get_s3_file(s3_url, filename=None):
    s3 = b3.resource('s3')
    bucket, s3_path = split_s3_url(s3_url)
    if filename is None:
        filename = os.path.basename(s3_path)
    else:
        path = os.path.dirname(filename)
        if not os.path.exists(path):
            os.makedirs(path)
    s3.Object(bucket, s3_path).download_file(filename)
    return filename


def split_s3_url(s3_url):
    """
    Return bucket, path

    Args:
        s3_url:

    Returns:
        (str, str)

    """
    s3_schemes = ["s3n", "s3"]
    url = urlparse(s3_url)
    if url.scheme not in s3_schemes:
        raise ValueError('Got an invalid URL scheme: Expected {}, got "{}" from "{}"'.format(' or '.join(s3_schemes), url.scheme, url.geturl()))
    bucket = url.hostname
    if bucket is None:
        raise ValueError('Got an empty S3 bucket (too many "/"s starting "{}"?)'.format(url.geturl()))
    key = url.path.lstrip('/')
    if len(key) == 0:
        key = None
    return bucket, key
