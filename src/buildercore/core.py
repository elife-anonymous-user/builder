"""this module appears to be where I collect functionality 
that is built upon by the more specialised parts of builder.

suggestions for a better name than 'core' welcome."""

import os, glob, json, re, copy
from os.path import join
from . import utils, config, project # BE SUPER CAREFUL OF CIRCULAR DEPENDENCIES
from .decorators import osissue, osissuefn, testme
import decorators
from .utils import first, second, dictfilter, lookup
from collections import OrderedDict
from functools import wraps
import boto
from boto.exception import BotoServerError
from contextlib import contextmanager
from fabric.api import settings
import importlib
import logging
from kids.cache import cache as cached
from slugify import slugify
import requests

LOG = logging.getLogger(__name__)

class DeprecationException(Exception):
    pass

class NoMasterException(Exception):
    pass

ALL_CFN_STATUS = [
    'CREATE_IN_PROGRESS',
    'CREATE_FAILED',
    'CREATE_COMPLETE',
    'ROLLBACK_IN_PROGRESS',
    'ROLLBACK_FAILED',
    'ROLLBACK_COMPLETE',
    'DELETE_IN_PROGRESS',
    'DELETE_FAILED',
    'UPDATE_IN_PROGRESS',
    'UPDATE_COMPLETE_CLEANUP_IN_PROGRESS',
    'UPDATE_COMPLETE',
    'UPDATE_ROLLBACK_IN_PROGRESS',
    'UPDATE_ROLLBACK_FAILED',
    'UPDATE_ROLLBACK_COMPLETE_CLEANUP_IN_PROGRESS',
    'UPDATE_ROLLBACK_COMPLETE',
]

ACTIVE_CFN_STATUS = [
    'CREATE_COMPLETE',
    'UPDATE_COMPLETE',
    'UPDATE_ROLLBACK_COMPLETE',
]

# non-transitioning 'steady states'
STEADY_CFN_STATUS = [
    'CREATE_FAILED',
    'CREATE_COMPLETE',
    'ROLLBACK_FAILED',
    'ROLLBACK_COMPLETE',
    'DELETE_FAILED',
    #'DELETE_COMPLETE', # technically true, but we can't do anything with them
    'UPDATE_COMPLETE',
    'UPDATE_ROLLBACK_FAILED',
    'UPDATE_ROLLBACK_COMPLETE',
]
    


#
# 
#

def connect_aws(service, region):
    "connects to given service using the region in the "
    aliases = {
        'cfn': 'cloudformation'
    }
    service = service if service not in aliases else aliases[service]
    conn = importlib.import_module('boto.%s' % service)
    return conn.connect_to_region(region)

@cached
def boto_cfn_conn(region):
    return connect_aws('cloudformation', region)

@cached
def boto_ec2_conn(region):
    return connect_aws('ec2', region)

@cached
def connect_aws_with_pname(pname, service):
    "convenience"
    pdata = project.project_data(pname)
    region = pdata['aws']['region']
    return connect_aws(service, region)

def connect_aws_with_stack(stackname, service):
    "convenience"
    pname = project_name_from_stackname(stackname)
    return connect_aws_with_pname(pname, service)

def find_ec2_instance(stackname):
    "returns ec2 instance data for a *specific* stackname"
    # filters: http://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_DescribeInstances.html
    kwargs = {
        'filters': {
            'tag:Name':[stackname],
            'instance-state-name': ['running']}}
    return connect_aws_with_stack(stackname, 'ec2').get_only_instances(**kwargs)

def find_ec2_volume(stackname):
    ec2_data = find_ec2_instance(stackname)[0]
    iid = ec2_data.id
    #http://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_DescribeVolumes.html
    kwargs = {'filters': {'attachment.instance-id': iid}}
    return connect_aws_with_stack(stackname, 'ec2').get_all_volumes(**kwargs)

# should live in `keypair`, but I can't have `core` depend on `keypair` and viceversa
def stack_pem(stackname, die_if_exists=False, die_if_doesnt_exist=False):
    """returns the path to the private key on the local filesystem. 
    helpfully dies in different ways if you ask it to"""
    expected_key = join(config.KEYPAIR_PATH, stackname + ".pem")
    # for when we really need it to exist
    if die_if_doesnt_exist and not os.path.exists(expected_key):
        raise EnvironmentError("keypair %r not found at %r" % (stackname, expected_key))
    if die_if_exists and os.path.exists(expected_key):
        raise EnvironmentError("keypair %r found at %r, not overwriting." % (stackname, expected_key))
    return expected_key

@contextmanager
def stack_conn(stackname, username=config.DEPLOY_USER, **kwargs):
    if 'user' in kwargs:
        LOG.warn("found key 'user' in given kwargs - did you mean 'username' ??")
    data = stack_data(stackname)
    public_ip = data['instance']['ip_address']
    params = {
        'user': username,
        'host_string': public_ip,
    }
    # doesn't hurt, handles cases where we want to establish a connection to run a task
    # when machine has failed to provision correctly.
    pem = stack_pem(stackname)
    if os.path.exists(pem) and username == config.BOOTSTRAP_USER:
        params.update({
            'key_filename': pem
        })
    params.update(kwargs)
    with settings(**params):
        yield

#
# stackname wrangling
#    

def mk_stackname(*bits):
    return "--".join(map(slugify, filter(None, bits)))

#TODO: test these functions
def parse_stackname(stackname):
    "returns a pair of project and cluster id"
    if not stackname or not isinstance(stackname, basestring):
        raise ValueError("stackname must look like <pname>--<cluster-id>, got: %r" % str(stackname))
    pname = cluster_id = None
    bits = stackname.split('--')
    # if len(bits) != 2:
    # for backward compatibility let's accept 3 bits for existing machine
    # in order to still be able to access them through ssh
    if len(bits) == 1:
        raise ValueError("could not parse given stackname %r" % stackname)
    return bits
        
def project_name_from_stackname(stackname):
    "returns just the project name from the given stackname"
    return first(parse_stackname(stackname))

def is_master_server_stack(stackname):
    return 'master-server--' in str(stackname)

def is_prod_stack(stackname):
    pname, cluster = parse_stackname(stackname)
    return cluster in ['master', 'prod']


#
# stack file wrangling
# stack 'files' are the things on the file system in the `.cfn/stacks/` dir.
# TODO: consider shifting .cfn/stacks/ to /temp/stacks.
# rendered files simply accumulate, are never consulted and JSON can be captured from AWS if needs be
#

def parse_stack_file_name(stack_filename):
    "returns just the stackname sans leading dirs and trailing extensions given a path to a stack"
    stack = os.path.basename(stack_filename) # just the file
    return os.path.splitext(stack)[0] # just the filename

def stack_files():
    "returns a list of manually created cloudformation stacknames"
    stacks = glob.glob("%s/*.json" % config.STACK_PATH)
    return map(parse_stack_file_name, stacks)

def stack_path(stackname, relative=False):
    "returns the full path to a stack JSON file given a stackname"
    if stackname in stack_files():
        path = config.STACK_DIR if relative else config.STACK_PATH
        return join(path, stackname) + ".json"
    raise ValueError("could not find stack %r in %r" % (stackname, config.STACK_PATH))

def stack_json(stackname, parse=False):
    "returns the json of the given stack as a STRING, not the parsed json unless `parse = True`."
    fp = open(stack_path(stackname), 'r')
    if parse:
        return json.load(fp)
    return fp.read()

#
# aws stack wrangling
# 'aws stacks' are stack files that have been given to AWS and provisioned.
#

# DO NOT CACHE. 
# this function is polled to get the state of the stack when creating/updating/deleting.
def describe_stack(stackname):
    "returns the full details of a stack given it's name or ID"
    return first(connect_aws_with_stack(stackname, 'cfn').describe_stacks(stackname))

# TODO: rename or something
def stack_data(stackname):
    """like `describe_stack`, but returns a dictionary with the Cloudformation 'outputs' 
    indexed by key and ec2 data under the key 'instance'"""
    stack = describe_stack(stackname)
    data = stack.__dict__
    if data.has_key('outputs'):
        data['indexed_output'] = {row.key: row.value for row in data['outputs']}
    try:
        # TODO: is there someway to go straight to the instance ID ?
        # a CloudFormation's outputs go stale! because we can't trust the data it
        # gives us, we sometimes take it's instance-id and talk to the instance directly.
        #inst_id = data['indexed_output']['InstanceId']
        #inst = get_instance(inst_id)
        stacks = find_ec2_instance(stackname)
        assert len(stacks) == 1, ("while looking for %s, found these stacks: %s" % (stackname, stacks))
        inst = stacks[0]
        data['instance'] = inst.__dict__
    except Exception:
        LOG.exception('caught an exception attempting to discover more information about this instance. The instance may not exist yet ...')
    return data


# DO NOT CACHE
def stack_is_active(stackname):
    "returns True if the given stack is in a completed state"
    try:
        description = describe_stack(stackname)
        result = description.stack_status in ['CREATE_COMPLETE', 'UPDATE_COMPLETE']
        if not result:
            LOG.info("stack_status is '%s'\nDescription: %s", description.stack_status, vars(description))
        return result
    except BotoServerError as err:
        if err.message.endswith('does not exist'):
            return False
        LOG.warning("unhandled exception testing active state of stack %r", stackname)
        raise

def stack_triple(aws_stack):
    "returns a triple of (name, status, data) of stacks."
    return (aws_stack.stack_name, aws_stack.stack_status, aws_stack)

#
# lists of aws stacks
#

@cached
def aws_stacks(region, status=[], formatter=stack_triple):
    "returns *all* stacks, even stacks deleted in the last 90 days"
    # NOTE: avoid `.describe_stack` as the results are truncated beyond a certain amount
    # use `.describe_stack` on specific stacks only
    results = boto_cfn_conn(region).list_stacks(status)
    if formatter:
        return map(formatter, results)
    return results

def active_aws_stacks(region, *args, **kwargs):
    "returns all stacks that are healthy"
    return aws_stacks(region, ACTIVE_CFN_STATUS, *args, **kwargs)

def steady_aws_stacks(region):
    "returns all stacks that are not in a transitionary state"
    return aws_stacks(region, STEADY_CFN_STATUS)

def active_aws_project_stacks(pname):
    "returns all active stacks for a given project name"
    pdata = project.project_data(pname)
    region = pdata['aws']['region']
    fn = lambda t: project_name_from_stackname(first(t)) == pname
    return filter(fn, active_aws_stacks(region))

def stack_names(stack_list):
    return sorted(map(first, stack_list))

def active_stack_names(region):
    "convenience. returns names of all active stacks"
    return stack_names(active_aws_stacks(region))

def steady_stack_names(region):
    "convenience. returns names of all stacks in a non-transitory state"
    return stack_names(steady_aws_stacks(region))

class MultipleRegionsError(EnvironmentError):
    def __init__(self, regions):
        self._regions = regions

    def regions(self):
        return self._regions

def find_region(stackname=None):
    """used when we haven't got a stack and need to know about stacks in a particular region.
    if a stack is provided, it uses the one provided in it's configuration.
    otherwise, generates a list of used regions from project data

    if more than one region available, it will raise an MultipleRegionsError.
    until we have some means of supporting multiple regions, this is the best solution"""
    if stackname:
        pdata = project_data_for_stackname(stackname)
        return pdata['aws']['region']

    all_projects = project.project_map()
    all_regions = [lookup(p, 'aws.region', None) for p in all_projects.values()]
    region_list = list(set(filter(None, all_regions))) # remove any Nones, make unique, make a list
    if not region_list:
        raise EnvironmentError("no regions available at all!")
    if len(region_list) > 1:
        raise MultipleRegionsError(region_list)
    return region_list[0]

@testme
def _find_master(sl):
    if len(sl) == 1:
        # first item (stackname) of first (and only) result
        return first(first(sl))
    
    msl = filter(lambda triple: is_master_server_stack(first(triple)), sl)
    msl = map(first, msl) # just stack names
    if len(msl) > 1:
        LOG.warn("more than one master server found: %s. this state should only ever be temporary.", msl)
    # this all assumes master servers with YMD instance ids
    #master_server_ymd_instance_id = lambda x: ''.join(x.split('--')[2:])
    #msl = sorted(msl, key=master_server_ymd_instance_id, reverse=True)
    msl = sorted(msl, key=parse_stackname, reverse=True)
    return first(msl)

def find_master(region):
    "returns the most recent aws master-server it can find. assumes instances have YMD names"
    sl = active_aws_stacks(region)
    if not sl:
        raise NoMasterException("no master servers found in region %r" % region)
    return _find_master(sl)

def find_master_for_stack(stackname):
    "convenience. finds the master server for the same region as given stack"
    pdata = project_data_for_stackname(stackname)
    return find_master(pdata['aws']['region'])

#
# decorators
#

def requires_active_stack(func):
    "requires a stack to exist in a successfully created/updated state"
    return decorators._requires_fn_stack(func, stack_is_active)

def requires_stack_file(func):
    "requires a stack template to exist on disk"
    msg = "I couldn't find a cloudformation stack file for %(stackname)r!"
    return decorators._requires_fn_stack(func, lambda stackname: stackname in stack_files(), msg)

#
#
#

def hostname_struct(stackname):
    "returns a dictionary with convenient domain name information"
    # wrangle hostname data

    pname, cluster = parse_stackname(stackname)
    pdata = project.project_data(pname)
    domain = pdata.get('domain')
    intdomain = pdata.get('intdomain')
    subdomain = pdata.get('subdomain')
    
    struct = {
        'domain': domain, # elifesciences.org
        'int_domain': intdomain, # elife.internal

        'subdomain': subdomain, # gateway
        
        'hostname': None, # temp.gateway

        'project_hostname': None, # gateway.elifesciences.org
        'int_project_hostname': None, # gateway.elife.internal
        
        'full_hostname': None, # gateway--temp.elifesciences.org
        'int_full_hostname': None, # gateway--temp.elife.internal
    }
    if not subdomain:
        # this project doesn't expect to be addressed
        # return immediately with what we do have
        return struct

    # removes any non-alphanumeric or hyphen characters
    subsubdomain = re.sub(r'[^\w\-]', '', cluster)
    hostname = subsubdomain + "--" + subdomain

    updates = {
        'hostname': hostname,

        'project_hostname': subdomain + "." + domain,
        'int_project_hostname': subdomain + "." + intdomain,

        'full_hostname': hostname + "." + domain,
        'int_full_hostname': hostname + "." + intdomain,
    }
    struct.update(updates)
    return struct

#
#
#

def project_data_for_stackname(stackname, *args, **kwargs):
    pname = project_name_from_stackname(stackname)
    return project.project_data(pname, *args, **kwargs)

#
# might be better off in bakery.py?
#

@testme
def ami_name(stackname):
    # elife-api.2015-12-31
    return "%s.%s" % (project_name_from_stackname(stackname), utils.ymd())
