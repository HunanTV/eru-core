# coding:utf-8

import json
import time
from more_itertools import chunked
from itertools import izip_longest
from celery import current_app
from flask import current_app as current_flask

from eru import consts
from eru.async import dockerjob
from eru.agent import get_agent
from eru.config import ERU_AGENT_API, DOCKER_REGISTRY
from eru.clients import rds
from eru.utils.notify import TaskNotifier
from eru.models import Container, Task, Network, Image

from eru.helpers.falcon import falcon_all_graphs, falcon_all_alarms, falcon_remove_alarms
from eru.helpers.check import wait_health_check

def add_container_backends(container):
    """单个container所拥有的后端服务
    HKEYS app_key 可以知道有哪些后端
    HGET 上面的结果可以知道后端都从哪里拿
    SMEMBERS entrypoint_key 可以拿出所有的后端
    """
    app_key = 'eru:app:{0}:backends'.format(container.appname)
    entrypoint_key = 'eru:app:{0}:entrypoint:{1}:backends'.format(container.appname, container.entrypoint)
    rds.hset(app_key, container.entrypoint, entrypoint_key)

    backends = container.get_backends()
    if backends:
        rds.sadd(entrypoint_key, *backends)

def remove_container_backends(container):
    """删除单个container的后端服务
    并不删除有哪些entrypoint, 这些service discovery方便知道哪些没了"""
    entrypoint_key = 'eru:app:{0}:entrypoint:{1}:backends'.format(container.appname, container.entrypoint)
    backends = container.get_backends()
    if backends:
        rds.srem(entrypoint_key, *backends)

# 删除在下面
def add_container_for_agent(container):
    """agent需要从key里取值出来去跟踪
    **改成了hashtable, agent需要更多的信息**
    另外key也改了, agent需要改下
    """
    host = container.host
    key = 'eru:agent:{0}:containers:meta'.format(host.name)
    rds.hset(key, container.container_id, json.dumps(container.meta))

def publish_to_service_discovery(*appnames):
    for appname in appnames:
        rds.publish('eru:discovery:published', appname)

@current_app.task()
def build_docker_image(task_id, base):
    task = Task.get(task_id)
    if not task:
        current_flask.logger.error('Task (id=%s) not found, quit', task_id)
        return

    current_flask.logger.info('Task<id=%s>: Start on host %s' % (task_id, task.host.ip))
    notifier = TaskNotifier(task)

    app = task.app
    host = task.host
    version = task.version

    try:
        repo, tag = base.split(':', 1)
        current_flask.logger.info('Task<id=%s>: Pull base image (base=%s)', task_id, base)
        notifier.store_and_broadcast(dockerjob.pull_image(host, repo, tag))

        current_flask.logger.info('Task<id=%s>: Build image (base=%s)', task_id, base)
        notifier.store_and_broadcast(dockerjob.build_image(host, version, base))

        current_flask.logger.info('Task<id=%s>: Push image (base=%s)', task_id, base)
        last_line = notifier.store_and_broadcast(dockerjob.push_image(host, version))
        dockerjob.remove_image(version, host)
    except Exception, e:
        task.finish(consts.TASK_FAILED)
        task.reason = e.message
        notifier.pub_fail()
        current_flask.logger.error('Task<id=%s>: Exception (e=%s)', task_id, e)
    else:
        # 粗暴的判断, 如果推送成功说明build成功
        if 'Digest: sha256' in last_line:
            task.finish(consts.TASK_SUCCESS)
            task.reason = 'ok'

            image_url = '%s/%s:%s' % (DOCKER_REGISTRY, app.name, version.short_sha)
            Image.create(app.id, version.id, image_url)

            notifier.pub_success()
        else:
            task.finish(consts.TASK_FAILED)
            task.reason = 'failed to push image to image hub'
            notifier.pub_fail()
        current_flask.logger.info('Task<id=%s>: Done', task_id)
    finally:
        notifier.pub_build_finish()

@current_app.task()
def remove_containers(task_id, cids, rmi=False):
    task = Task.get(task_id)
    if not task:
        current_flask.logger.error('Task (id=%s) not found, quit', task_id)
        return

    current_flask.logger.info('Task<id=%s>: Start on host %s' % (task_id, task.host.ip))
    notifier = TaskNotifier(task)
    containers = Container.get_multi(cids)
    container_ids = [c.container_id for c in containers if c]
    host = task.host
    version = task.version
    try:
        # flag, don't report these
        flags = {'eru:agent:%s:container:flag' % cid: 1 for cid in container_ids}
        rds.mset(**flags)
        for c in containers:
            remove_container_backends(c)
            current_flask.logger.info('Task<id=%s>: Container (cid=%s) backends removed',
                    task_id, c.container_id[:7])
        appnames = {c.appname for c in containers}
        publish_to_service_discovery(*appnames)

        time.sleep(3)

        dockerjob.remove_host_containers(containers, host)
        current_flask.logger.info('Task<id=%s>: Containers (cids=%s) removed', task_id, cids)
        if rmi:
            try:
                dockerjob.remove_image(task.version, host)
            except Exception as e:
                current_flask.logger.error('Task<id=%s>: Exception (e=%s), fail to remove image', task_id, e)
    except Exception as e:
        task.finish(consts.TASK_FAILED)
        task.reason = e.message
        notifier.pub_fail()
        current_flask.logger.error('Task<id=%s>: Exception (e=%s)', task_id, e)
    else:
        for c in containers:
            c.delete()
        task.finish(consts.TASK_SUCCESS)
        task.reason = 'ok'
        notifier.pub_success()
        if container_ids:
            rds.hdel('eru:agent:%s:containers:meta' % host.name, *container_ids)
        rds.delete(*flags.keys())
        current_flask.logger.info('Task<id=%s>: Done', task_id)

    if not version.containers.count():
        falcon_remove_alarms(version)

def _iter_cores(cores, ncontainer):
    full_cores, part_cores = cores.get('full', []), cores.get('part', [])
    if not (full_cores or part_cores):
        return (([], []) for _ in range(ncontainer))

    return izip_longest(
        chunked(full_cores, len(full_cores)/ncontainer),
        chunked(part_cores, len(part_cores)/ncontainer),
        fillvalue=[]
    )

@current_app.task()
def create_containers_with_macvlan(task_id, ncontainer, nshare, cores, network_ids, spec_ips=None):
    """
    执行task_id的任务. 部署ncontainer个容器, 占用*_core_ids这些核, 绑定到networks这些子网
    """
    current_flask.logger.info('Task<id=%s>: Started', task_id)
    task = Task.get(task_id)
    if not task:
        current_flask.logger.error('Task (id=%s) not found, quit', task_id)
        return

    if spec_ips is None:
        spec_ips = []

    need_network = bool(network_ids)
    networks = Network.get_multi(network_ids)

    notifier = TaskNotifier(task)
    host = task.host
    version = task.version
    entrypoint = task.props['entrypoint']
    env = task.props['env']
    ports = task.props['ports']
    args = task.props['args']
    # use raw
    route = task.props['route']
    image = task.props['image']
    callback_url = task.props['callback_url']
    cpu_shares = int(float(nshare) / host.pod.core_share * 1024) if nshare else 1024

    pub_agent_vlan_key = 'eru:agent:%s:vlan' % host.name
    pub_agent_route_key = 'eru:agent:%s:route' % host.name
    feedback_key = 'eru:agent:%s:feedback' % task_id

    cids = []
    backends = []
    entry = version.appconfig.entrypoints[entrypoint]

    for fcores, pcores in _iter_cores(cores, ncontainer):
        cores_for_one_container = {'full': fcores, 'part': pcores}
        try:
            cid, cname = dockerjob.create_one_container(host, version,
                entrypoint, env, fcores+pcores, ports=ports, args=args,
                cpu_shares=cpu_shares, image=image, need_network=need_network)
        except Exception as e:
            # 写给celery日志看
            print e
            host.release_cores(cores_for_one_container, nshare)
            continue

        if spec_ips:
            ips = [n.acquire_specific_ip(ip) for n, ip in zip(networks, spec_ips)]
        else:
            ips = [n.acquire_ip() for n in networks]
        ips = [i for i in ips if i]
        ip_dict = {ip.vlan_address: ip for ip in ips}

        if ips:
            if ERU_AGENT_API == 'pubsub':
                values = [str(task_id), cid] + ['{0}:{1}'.format(ip.vlan_seq_id, ip.vlan_address) for ip in ips]
                rds.publish(pub_agent_vlan_key, '|'.join(values))
            elif ERU_AGENT_API == 'http':
                agent = get_agent(host)
                ip_list = [(ip.vlan_seq_id, ip.vlan_address) for ip in ips]
                agent.add_container_vlan(cid, str(task_id), ip_list)

        for _ in ips:
            # timeout 15s
            rv = rds.blpop(feedback_key, 15)
            if rv is None:
                break
            # rv is like (feedback_key, 'succ|container_id|vethname|ip')
            succ, _, vethname, vlan_address = rv[1].split('|')
            if succ == '0':
                break
            ip = ip_dict.get(vlan_address, None)
            if ip:
                ip.set_vethname(vethname)

            if route:
                rds.publish(pub_agent_route_key, '%s|%s' % (cid, route))

        else:
            current_flask.logger.info('Creating container (cid=%s, ips=%s)', cid, ips)
            c = Container.create(cid, host, version, cname, entrypoint,
                    cores_for_one_container, env, nshare, callback_url)
            for ip in ips:
                ip.assigned_to_container(c)
            notifier.notify_agent(c)
            add_container_for_agent(c)
            add_container_backends(c)
            cids.append(cid)
            backends.extend(c.get_backends())
            # 略过清理工作
            continue

        # 清理掉失败的容器, 释放核, 释放ip
        current_flask.logger.info('Cleaning failed container (cid=%s)', cid)
        dockerjob.remove_container_by_cid([cid], host)
        host.release_cores(cores_for_one_container, nshare)
        [ip.release() for ip in ips]
        # 失败了就得清理掉这个key
        rds.delete(feedback_key)

    health_check = entry.get('health_check', '')
    if health_check and backends:
        urls = [b + health_check for b in backends]
        if not wait_health_check(urls):
            # TODO 这里要么回滚要么报警
            current_flask.logger.info('Task<id=%s>: Done, but something went error', task_id)
            return

    publish_to_service_discovery(version.name)
    task.finish(consts.TASK_SUCCESS)
    task.reason = 'ok'
    task.container_ids = cids
    notifier.pub_success()

    # 有IO, 丢最后面算了
    falcon_all_graphs(version)
    falcon_all_alarms(version)

    current_flask.logger.info('Task<id=%s>: Done', task_id)
