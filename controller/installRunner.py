#!/usr/bin/env python3
# encoding: utf-8

import os
import sys
import shutil
import json
import ansible_runner
import collections
import logging
from kubernetes import client, config

'''
playbookBasePath: The folder where the playbooks is located.
privateDataDir: The folder where the playbooks execution results are located.
configFile: Define the parameters in the installation process. Generated by cluster configuration
statusFile: Define the status in the installation process.
'''
playbookBasePath = '/kubesphere/playbooks'
privateDataDir = '/kubesphere/results'
configFile = '/kubesphere/config/ks-config.json'
statusFile = '/kubesphere/config/ks-status.json'

logging.basicConfig(level=logging.INFO, format="%(message)s")

ks_hook = '''
{
	"onKubernetesEvent": [{
		"name": "Monitor clusterconfiguration",
		"kind": "ClusterConfiguration",
		"event": [ "add", "update" ],
		"objectName": "ks-installer",
		"namespaceSelector": {
			"matchNames": ["kubesphere-system"]
		},
		"jqFilter": ".spec",
		"allowFailure": false
	}]
}
'''

cluster_configuration = {
    "apiVersion": "installer.kubesphere.io/v1alpha1",
    "kind": "ClusterConfiguration",
    "metadata": {
        "name": "ks-installer",
        "namespace": "kubesphere-system",
        "labels": {
            "version": "master"
        },
    },
}

# Define components to install


class component():

    def __init__(
            self,
            playbook,
            private_data_dir,
            artifact_dir,
            ident,
            quiet,
            rotate_artifacts):
        '''
        :param private_data_dir: The directory containing all runner metadata needed to invoke the runner
                                 module. Output artifacts will also be stored here for later consumption.
        :param ident: The run identifier for this invocation of Runner. Will be used to create and name
                      the artifact directory holding the results of the invocation.
        :param playbook: The playbook that will be invoked by runner when executing Ansible.
        :param artifact_dir: The path to the directory where artifacts should live, this defaults to 'artifacts' under the private data dir
        :param quiet: Disable all output
        '''

        self.playbook = playbook
        self.private_data_dir = private_data_dir
        self.artifact_dir = artifact_dir
        self.ident = ident
        self.quiet = quiet
        self.rotate_artifacts = rotate_artifacts

    # Generate ansible_runner objects based on parameters

    def installRunner(self):
        installer = ansible_runner.run_async(
            playbook=self.playbook,
            private_data_dir=self.private_data_dir,
            artifact_dir=self.artifact_dir,
            ident=self.ident,
            quiet=self.quiet,
            rotate_artifacts=self.rotate_artifacts
        )
        return installer[1]


# Using the Observer pattern to get the info of task execution

class Subject(object):

    def __init__(self):
        self._observers = []

    def attach(self, observer):
        if observer not in self._observers:
            self._observers.append(observer)

    def detach(self, observer):
        try:
            self._observers.remove(observer)
        except ValueError:
            pass

    def notify(self, modifier=None):
        for observer in self._observers:
            if modifier != observer:
                observer.update(self)


class Info(Subject):

    def __init__(self, name=''):
        Subject.__init__(self)
        self.name = name
        self._info = None

    @property
    def info(self):
        return self._info

    @info.setter
    def info(self, value):
        self._info = value
        self.notify()


class InfoViewer:
    def update(self, subject):
        logging.info(u'%s' % (subject.info))


infoGetter = Info('taskInfo')
viewer = InfoViewer()
infoGetter.attach(viewer)


def get_cluster_configuration(api):
    resource = api.get_namespaced_custom_object(
        group="installer.kubesphere.io",
        version="v1alpha1",
        name="ks-installer",
        namespace="kubesphere-system",
        plural="clusterconfigurations",
    )

    return resource


def create_cluster_configuration(api, resource):
    api.create_namespaced_custom_object(
        group="installer.kubesphere.io",
        version="v1alpha1",
        namespace="kubesphere-system",
        plural="clusterconfigurations",
        body=resource,
    )

    logging.info("Create cluster configuration successfully")


def delete_cluster_configuration(api):
    api.delete_namespaced_custom_object(
        group="installer.kubesphere.io",
        version="v1alpha1",
        name="ks-installer",
        namespace="kubesphere-system",
        plural="clusterconfigurations",
    )

    logging.info("Delete old cluster configuration successfully")


def getResultInfo():
    # Execute and add the installation task process
    taskProcessList = []
    tasks = generateTaskLists()
    for taskName, taskObject in tasks.items():
        taskProcess = {}
        infoGetter.info = "Start installing {}".format(taskName)
        taskProcess[taskName] = taskObject.installRunner()
        taskProcessList.append(
            taskProcess
        )

    taskProcessListLen = len(taskProcessList)
    logging.info('*' * 50)
    logging.info('Waiting for all tasks to be completed ...')
    completedTasks = []
    while True:
        for taskProcess in taskProcessList:
            taskName = list(taskProcess.keys())[0]
            result = taskProcess[taskName].rc
            if result is not None and {taskName: result} not in completedTasks:
                infoGetter.info = "task {} status is {}  ({}/{})".format(
                    taskName,
                    taskProcess[taskName].status,
                    len(completedTasks) + 1,
                    len(taskProcessList)
                )
                completedTasks.append({taskName: result})

        if len(completedTasks) == taskProcessListLen:
            break
    logging.info('*' * 50)
    logging.info('Collecting installation results ...')

    # Operation result check
    resultState = False
    for taskResult in completedTasks:
        taskName = list(taskResult.keys())[0]
        taskRC = list(taskResult.values())[0]

        if taskRC != 0:
            resultState = resultState or True
            resultInfoPath = os.path.join(
                privateDataDir,
                str(taskName),
                str(taskName),
                'job_events'
            )
            if os.path.exists(resultInfoPath):
                jobList = os.listdir(resultInfoPath)
                jobList.sort(
                    key=lambda x: int(x.split('-')[0])
                )

                errorEventFile = os.path.join(resultInfoPath, jobList[-2])
                with open(errorEventFile, 'r') as f:
                    failedEvent = json.load(f)
                print("\n")
                print("Task '{}' failed:".format(taskName))
                print('*' * 150)
                print(json.dumps(failedEvent, sort_keys=True, indent=2))
                print('*' * 150)
    return resultState


# Generate a objects list of components


def generateTaskLists():
    readyToEnabledList, readyToDisableList = getComponentLists()
    tasksDict = {}
    for taskName in readyToEnabledList:
        playbookPath = os.path.join(playbookBasePath, str(taskName) + '.yaml')
        artifactDir = os.path.join(privateDataDir, str(taskName))
        if os.path.exists(artifactDir):
            shutil.rmtree(artifactDir)

        tasksDict[str(taskName)] = component(
            playbook=playbookPath,
            private_data_dir=privateDataDir,
            artifact_dir=artifactDir,
            ident=str(taskName),
            quiet=True,
            rotate_artifacts=1
        )

    return tasksDict

# Generate a list of components to install based on the configuration file


def getComponentLists():
    readyToEnabledList = [
        'monitoring',
        'multicluster',
        'openpitrix',
        'network']
    readyToDisableList = []
    global configFile

    if os.path.exists(configFile):
        with open(configFile, 'r') as f:
            configs = json.load(f)
        f.close()
    else:
        print("The configuration file does not exist !  {}".format(configFile))
        exit()

    for component, parameters in configs.items():
        if (not isinstance(parameters, str)) or (
                not isinstance(parameters, int)):
            try:
                for j, value in parameters.items():
                    if (j == 'enabled') and (value):
                        readyToEnabledList.append(component)
                        break
                    elif (j == 'enabled') and (value == False):
                        readyToDisableList.append(component)
                        break
            except BaseException:
                pass
    try:
        readyToEnabledList.remove("metrics_server")
    except BaseException:
        pass

    try:
        readyToEnabledList.remove("networkpolicy")
    except BaseException:
        pass

    try:
        readyToEnabledList.remove("telemetry")
    except BaseException:
        pass

    return readyToEnabledList, readyToDisableList


def preInstallTasks():
    preInstallTasks = collections.OrderedDict()
    preInstallTasks['preInstall'] = [
        os.path.join(playbookBasePath, 'preinstall.yaml'),
        os.path.join(privateDataDir, 'preinstall')
    ]
    preInstallTasks['metrics-server'] = [
        os.path.join(playbookBasePath, 'metrics_server.yaml'),
        os.path.join(privateDataDir, 'metrics_server')
    ]
    preInstallTasks['common'] = [
        os.path.join(playbookBasePath, 'common.yaml'),
        os.path.join(privateDataDir, 'common')
    ]
    preInstallTasks['ks-core'] = [
        os.path.join(playbookBasePath, 'ks-core.yaml'),
        os.path.join(privateDataDir, 'ks-core')
    ]

    for task, paths in preInstallTasks.items():
        pretask = ansible_runner.run(
            playbook=paths[0],
            private_data_dir=privateDataDir,
            artifact_dir=paths[1],
            ident=str(task),
            quiet=False
        )
        if pretask.rc != 0:
            exit()


def resultInfo(resultState=False, api=None):
    ks_config = ansible_runner.run(
        playbook=os.path.join(playbookBasePath, 'ks-config.yaml'),
        private_data_dir=privateDataDir,
        artifact_dir=os.path.join(privateDataDir, 'ks-config'),
        ident='ks-config',
        quiet=True
    )

    if ks_config.rc != 0:
        print("Failed to ansible-playbook ks-config.yaml")
        exit()

    result = ansible_runner.run(
        playbook=os.path.join(playbookBasePath, 'result-info.yaml'),
        private_data_dir=privateDataDir,
        artifact_dir=os.path.join(privateDataDir, 'result-info'),
        ident='result',
        quiet=True
    )

    if result.rc != 0:
        print("Failed to ansible-playbook result-info.yaml")
        exit()

    resource = get_cluster_configuration(api)

    if "migration" in resource['status']['core'] and resource['status']['core']['migration'] and resultState == False:
        migration = ansible_runner.run(
            playbook=os.path.join(playbookBasePath, 'ks-migration.yaml'),
            private_data_dir=privateDataDir,
            artifact_dir=os.path.join(privateDataDir, 'ks-migration'),
            ident='ks-migration',
            quiet=False
        )
        if migration.rc != 0:
            exit()

    if not resultState:
        with open(os.path.join(playbookBasePath, 'kubesphere_running'), 'r') as f:
            info = f.read()
            logging.info(info)

    telemeter = ansible_runner.run(
        playbook=os.path.join(playbookBasePath, 'telemetry.yaml'),
        private_data_dir=privateDataDir,
        artifact_dir=os.path.join(privateDataDir, 'telemetry'),
        ident='telemetry',
        quiet=True
    )

    if telemeter.rc != 0:
        exit()


def generateConfig(api):

    resource = get_cluster_configuration(api)

    cluster_config = resource['spec']

    api = client.CoreV1Api()
    nodes = api.list_node(_preload_content=False)
    nodesStr = nodes.read().decode('utf-8')
    nodesObj = json.loads(nodesStr)

    cluster_config['nodeNum'] = len(nodesObj["items"])
    cluster_config['kubernetes_version'] = client.VersionApi().get_code().git_version

    try:
        with open(configFile, 'w', encoding='utf-8') as f:
            json.dump(cluster_config, f, ensure_ascii=False, indent=4)
    except BaseException:
        with open(configFile, 'w', encoding='utf-8') as f:
            json.dump({"config": "new"}, f, ensure_ascii=False, indent=4)

    try:
        with open(statusFile, 'w', encoding='utf-8') as f:
            json.dump({"status": resource['status']},
                      f, ensure_ascii=False, indent=4)
    except BaseException:
        with open(statusFile, 'w', encoding='utf-8') as f:
            json.dump({"status": {"enabledComponents": []}},
                      f, ensure_ascii=False, indent=4)

# Migrate cluster configuration


def generate_new_cluster_configuration(api):
    global old_cluster_configuration
    upgrade_flag = False
    try:
        old_cluster_configuration = get_cluster_configuration(api)
    except BaseException:
        exit(0)

    cluster_configuration_spec = old_cluster_configuration.get('spec')
    cluster_configuration_status = old_cluster_configuration.get('status')

    if "common" in cluster_configuration_spec:
        if "mysqlVolumeSize" in cluster_configuration_spec["common"]:
            del cluster_configuration_spec["common"]["mysqlVolumeSize"]
        if "etcdVolumeSize" in cluster_configuration_spec["common"]:
            del cluster_configuration_spec["common"]["etcdVolumeSize"]
        if cluster_configuration_status is not None and "redis" in cluster_configuration_status and "status" in cluster_configuration_status[
                "redis"] and cluster_configuration_status["redis"]["status"] == "enabled":
            cluster_configuration_spec["common"]["redis"] = {
                "enabled": True
            }
        else:
            cluster_configuration_spec["common"]["redis"] = {
                "enabled": False
            }

        if cluster_configuration_status is not None and "openldap" in cluster_configuration_status and "status" in cluster_configuration_status[
                "openldap"] and cluster_configuration_status["openldap"]["status"] == "enabled":
            cluster_configuration_spec["common"]["openldap"] = {
                "enabled": True
            }
        else:
            cluster_configuration_spec["common"]["openldap"] = {
                "enabled": False
            }

        if "redisVolumSize" in cluster_configuration_spec["common"]:
            cluster_configuration_spec["common"]["redis"][
                "volumeSize"] = cluster_configuration_spec["common"]["redisVolumSize"]
            del cluster_configuration_spec["common"]["redisVolumSize"]
        if "openldapVolumeSize" in cluster_configuration_spec["common"]:
            cluster_configuration_spec["common"]["openldap"][
                "volumeSize"] = cluster_configuration_spec["common"]["openldapVolumeSize"]
            del cluster_configuration_spec["common"]["openldapVolumeSize"]
        if "minio" not in cluster_configuration_spec["common"]:
            if "minioVolumeSize" in cluster_configuration_spec["common"]:
                cluster_configuration_spec["common"]["minio"] = {
                    "volumeSize": cluster_configuration_spec["common"]["minioVolumeSize"]
                }
                del cluster_configuration_spec["common"]["minioVolumeSize"]
        else:
            if "minioVolumeSize" in cluster_configuration_spec["common"]:
                cluster_configuration_spec["common"]["minio"]["volumeSize"] = cluster_configuration_spec["common"]["minioVolumeSize"]
                del cluster_configuration_spec["common"]["minioVolumeSize"]

        # Migrate the configuration of es elasticsearch
        if "es" in cluster_configuration_spec["common"]:
            if "master" not in cluster_configuration_spec["common"]["es"]:
                cluster_configuration_spec["common"]["es"]["master"] = {
                    "volumeSize": "4Gi"
                }
            if "data" not in cluster_configuration_spec["common"]["es"]:
                cluster_configuration_spec["common"]["es"]["data"] = {
                    "volumeSize": "20Gi"
                }
            if "elasticsearchMasterReplicas" in cluster_configuration_spec["common"]["es"]:
                cluster_configuration_spec["common"]["es"]["master"]["replicas"] = cluster_configuration_spec["common"]["es"]["elasticsearchMasterReplicas"]
                del cluster_configuration_spec["common"]["es"]["elasticsearchMasterReplicas"]
            if "elasticsearchDataReplicas" in cluster_configuration_spec["common"]["es"]:
                cluster_configuration_spec["common"]["es"]["data"]["replicas"] = cluster_configuration_spec["common"]["es"]["elasticsearchDataReplicas"]
                del cluster_configuration_spec["common"]["es"]["elasticsearchDataReplicas"]
            if "elasticsearchMasterVolumeSize" in cluster_configuration_spec["common"]["es"]:
                cluster_configuration_spec["common"]["es"]["master"]["volumeSize"] = cluster_configuration_spec["common"]["es"]["elasticsearchMasterVolumeSize"]
                del cluster_configuration_spec["common"]["es"]["elasticsearchMasterVolumeSize"]
            if "elasticsearchDataVolumeSize" in cluster_configuration_spec["common"]["es"]:
                cluster_configuration_spec["common"]["es"]["data"]["volumeSize"] = cluster_configuration_spec["common"]["es"]["elasticsearchDataVolumeSize"]
                del cluster_configuration_spec["common"]["es"]["elasticsearchDataVolumeSize"]
            if "externalElasticsearchHost" not in cluster_configuration_spec["common"]["es"] and "externalElasticsearchUrl" in cluster_configuration_spec["common"]["es"]:
                cluster_configuration_spec["common"]["es"]["externalElasticsearchHost"] = cluster_configuration_spec["common"]["es"]["externalElasticsearchUrl"]

        if "console" in cluster_configuration_spec:
            if "core" in cluster_configuration_spec["common"]:
                cluster_configuration_spec["common"]["core"]["console"]=cluster_configuration_spec["console"]
            else:
                cluster_configuration_spec["common"]["core"] = {
                    "console": cluster_configuration_spec["console"]
                }
            del cluster_configuration_spec["console"]

    if "logging" in cluster_configuration_spec and "logsidecarReplicas" in cluster_configuration_spec[
            "logging"]:
        upgrade_flag = True
        if "enabled" in cluster_configuration_spec["logging"]:
            if cluster_configuration_spec["logging"]["enabled"]:
                cluster_configuration_spec["logging"] = {
                    "enabled": True,
                    "logsidecar": {
                        "enabled": True,
                        "replicas": 2
                    }
                }
            else:
                cluster_configuration_spec["logging"] = {
                    "enabled": False,
                    "logsidecar": {
                        "enabled": False,
                        "replicas": 2
                    }
                }

    if "notification" in cluster_configuration_spec:
        upgrade_flag = True
        del cluster_configuration_spec['notification']

    if "openpitrix" in cluster_configuration_spec and "store" not in cluster_configuration_spec[
            "openpitrix"]:
        upgrade_flag = True
        if "enabled" in cluster_configuration_spec["openpitrix"]:
            if cluster_configuration_spec["openpitrix"]["enabled"]:
                cluster_configuration_spec["openpitrix"] = {
                    "store": {
                        "enabled": True
                    }
                }
            else:
                cluster_configuration_spec["openpitrix"] = {
                    "store": {
                        "enabled": False
                    }
                }

    if "networkpolicy" in cluster_configuration_spec:
        upgrade_flag = True
        if "enabled" in cluster_configuration_spec[
                "networkpolicy"] and cluster_configuration_spec["networkpolicy"]["enabled"]:
            cluster_configuration_spec["network"] = {
                "networkpolicy": {
                    "enabled": True,
                },
                "ippool": {
                    "type": "none",
                },
                "topology": {
                    "type": "none",
                },
            }
        else:
            cluster_configuration_spec["network"] = {
                "networkpolicy": {
                    "enabled": False,
                },
                "ippool": {
                    "type": "none",
                },
                "topology": {
                    "type": "none",
                },
            }
        del cluster_configuration_spec["networkpolicy"]

    # add edgeruntime configuration migration
    if "kubeedge" in cluster_configuration_spec:
        upgrade_flag = True
        if "enabled" in cluster_configuration_spec["kubeedge"]:
            cluster_configuration_spec["edgeruntime"] = {
                "enabled": cluster_configuration_spec["kubeedge"]["enabled"],
                "kubeedge": cluster_configuration_spec["kubeedge"]
            }

            cluster_configuration_spec["edgeruntime"]["kubeedge"]["iptables-manager"] = {
                "enabled": True,
                "mode": "external"
            }

        try:
            del cluster_configuration_spec["edgeruntime"]["kubeedge"]["edgeWatcher"]
        except:
            pass
        
        del cluster_configuration_spec["kubeedge"]

    if isinstance(cluster_configuration_status,
                  dict) and "core" in cluster_configuration_status:
        if ("version" in cluster_configuration_status["core"] and cluster_configuration_status["core"]["version"] !=
                cluster_configuration["metadata"]["labels"]["version"]) or "version" not in cluster_configuration_status["core"]:
            upgrade_flag = True

    if upgrade_flag:
        cluster_configuration["spec"] = cluster_configuration_spec
        if isinstance(cluster_configuration_status,
                      dict) and "clusterId" in cluster_configuration_status:
            cluster_configuration["status"] = {
                "clusterId": cluster_configuration_status["clusterId"]
            }
        delete_cluster_configuration(api)
        create_cluster_configuration(api, cluster_configuration)
        exit(0)


def main():
    global privateDataDir, playbookBasePath, configFile, statusFile

    if len(sys.argv) > 1 and sys.argv[1] == "--config":
        print(ks_hook)
        return

    if len(sys.argv) > 1 and sys.argv[1] == "--debug":
        privateDataDir = os.path.abspath('./results')
        playbookBasePath = os.path.abspath('./playbooks')
        configFile = os.path.abspath('./results/ks-config.json')
        statusFile = os.path.abspath('./results/ks-status.json')
        config.load_kube_config()
    else:
        config.load_incluster_config()

    if not os.path.exists(privateDataDir):
        os.makedirs(privateDataDir)

    api = client.CustomObjectsApi()
    generate_new_cluster_configuration(api)
    generateConfig(api)
    # execute preInstall tasks
    preInstallTasks()
    resultState = getResultInfo()
    resultInfo(resultState, api)


if __name__ == '__main__':
    main()
