import argparse
import json
import logging
import os
import pexpect
import pkg_resources
import signal

KUBERNETES_EPHEMERAL_CONTAINER_VER = "v1.25.0"

class ExecutableNotFound(Exception):
    def __init__(self, execName):
        super().__init__("There is no {} in the PATH".format(execName))

class KubectlError(Exception):
    def __init__(self, message):
        super().__init__("Kubectl command error:" + message)

class ParseError(Exception):
    def __init__(self, message):
        super().__init__("Parse error:" + message)

class BuildStaticBinaryException(Exception):
    def __init__(self, message):
        super().__init__("Cannot create static binary: " + message)

class GDBCommandException(Exception):
    def __init__(self, message):
        super().__init__("GDB command error: " + message)


def GetKubernetesVersion(kubectl_cmd):
    logging.debug("Getting the Kubernetes server version")
    output, err = pexpect.run(kubectl_cmd + " version --output=json", withexitstatus=True)
    if err != 0:
        raise KubectlError("Cannot get kubernetes version")

    j = json.loads(output)

    if "serverVersion" in j and "gitVersion" in j["serverVersion"]:
        version = j["serverVersion"]["gitVersion"]
        logging.debug("The kubernetes server version is: " + version)
        return version

    logging.warning("Cannot get the kubernetes version")
    return ""

class K8sGDBServer():
    def __init__(self, args):
        self.args = args
        self.portForwardChild = None
        self.gdbServerCmd = ""
        self.gdbServerChild = None

        self.version = GetKubernetesVersion(args.kubectl_cmd)

        self.local_port = ""
        if args.local_port:
            self.local_port = args.local_port

    def StartDebug(self):
        self.GetContainerName()

        if pkg_resources.parse_version(self.version) >= pkg_resources.parse_version(KUBERNETES_EPHEMERAL_CONTAINER_VER):
            self.PrepareWithEphemeralContainer()
        else:
            if not self.IsExecutableInContainerImage("tar"):
                if not self.IsExecutableInContainerImage("tee"):
                    logging.critical("Cannot move gdbserver into the container")
                    exit(-1)
                else:
                    self.TryToAddTarExecutable()

            self.PrepareWithKubectlCP()

        self.StartPortForward()
        try:
            self.StartGDBServer()
        except:
            self.StopDebug()
            raise

        logging.info("Port fordwarded gdbserver is listening on localhost:" + str(self.local_port))

    def StopDebug(self):
        logging.info("Stopping debug")
        self.StopGDBServer()
        self.StopPortForward()

    def PrepareWithEphemeralContainer(self):
        logging.info("Ephemeral containers are supported, so using 'kubectl debug' for gdbserver")

        self.gdbServerCmd = "{kubectl} debug -n {namespace} {pod} --image=albertdupre/gdbserver:latest --target={pod} -i -- sh -c 'sleep 5 ; gdbserver --attach localhost:{port} {pid}'".format(
            kubectl=self.args.kubectl_cmd,
            namespace=self.args.namespace,
            pod=self.args.pod,
            port=self.args.remote_port,
            pid=self.args.pid)

    def PrepareWithKubectlCP(self):
        logging.info("Using the kubectl cp for setting up gdbserver")

        if not self.IsExecutableInContainerImage('gdbserver'):
            logging.debug("'gdbserver' is not present on the container")

            self.BuildStaticBinary("gdbserver")

            logging.debug("Copying the 'gdbserver' binary to the container")
            output, err = pexpect.run("{kubectl} cp ./gdbserver {namespace}/{pod}:/bin/gdbserver -c {container}".format(
                kubectl=self.args.kubectl_cmd,
                namespace=self.args.namespace,
                pod=self.args.pod,
                container=self.args.container), withexitstatus=True)
            if err != 0:
                raise KubectlError("Cannot cp into the container: " + output.decode("utf-8"))

        self.gdbServerCmd = "{kubectl} exec -n {namespace} {pod} -c {container} -- gdbserver --attach localhost:{port} {pid}".format(
            kubectl=self.args.kubectl_cmd,
            namespace=self.args.namespace,
            pod=self.args.pod,
            container=self.args.container,
            port=self.args.remote_port,
            pid=self.args.pid)

    def IsExecutableInContainerImage(self, execName):
        logging.debug("Finding out that the {} is present in container".format(execName))
        output, err = pexpect.run("{kubectl} exec -n {namespace} {pod} -c {container} -- {execName} --help".format(
            kubectl=self.args.kubectl_cmd,
            namespace=self.args.namespace,
            pod=self.args.pod,
            container=self.args.container,
            execName=execName), withexitstatus=True)

        if err != 0:
            logging.warning("{} is not present in container".format(execName))
            return False

        logging.debug("{} is present in container".format(execName))
        return True

    def TryToAddTarExecutable(self):
        logging.debug("Trying to add static 'tar' executable to the container")

        self.BuildStaticBinary("tar")

        logging.debug("Copying the 'tar' executable to the container")
        output, err = pexpect.run("sh -c 'cat ./tar | {kubectl} exec -i -n {namespace} {pod} -c {container} -- tee /bin/tar'".format(
            kubectl=self.args.kubectl_cmd,
            namespace=self.args.namespace,
            pod=self.args.pod,
            container=self.args.container), withexitstatus=True)
        if err != 0:
            raise KubectlError("Error during sending tar executable to remote host: " + output.decode("utf-8"))

        logging.debug("Adding executable attribute to the 'tar' in the container")
        output, err = pexpect.run("{kubectl} exec -n {namespace} {pod} -c {container} -- chmod +x /bin/tar".format(
            kubectl=self.args.kubectl_cmd,
            namespace=self.args.namespace,
            pod=self.args.pod,
            container=self.args.container), withexitstatus=True)
        if err != 0:
            raise KubectlError("Error during chmod of tar: " + output.decode("utf-8"))

    def StartPortForward(self):
        logging.info("Starting the port forward to the container")
        self.portForwardChild = pexpect.spawn("{kubectl} port-forward -n {namespace} pod/{pod} {local_port}:{remote_port}".format(
            kubectl=self.args.kubectl_cmd,
            namespace=self.args.namespace,
            pod=self.args.pod,
            local_port=self.local_port,
            remote_port=self.args.remote_port))

        self.portForwardChild.expect("Forwarding from .* ->")

        self.local_port = self.portForwardChild.after.decode("utf-8").splitlines()[0].split(":")[1].split(" -> ")[0]
        logging.debug("The local gdbserver port is " + self.local_port)

    def StopPortForward(self):
        logging.debug("Sending SIGINT for portforward")
        self.portForwardChild.sendintr()
        self.portForwardChild.wait()
        logging.debug("Portforward stopped")

    def StartGDBServer(self):
        logging.info("Starting gdbserver")
        self.gdbServerChild = pexpect.spawn(self.gdbServerCmd)

        index = self.gdbServerChild.expect([".*Listening on port ", ".*Can't bind address: Address in use."])
        if index == 0:
            logging.debug("The remote gdbserver port is " + str(self.args.remote_port))
        else:
            logging.error("The port is occupied")
            raise GDBCommandException("Address in use")

    def StopGDBServer(self):
        logging.info("Stopping gdbserver")
        self.StopGDBServerRemotely()

        if self.gdbServerChild.isalive():
            logging.debug("Sending SIGINT to local gdbserver session")
            self.gdbServerChild.sendintr()
            self.gdbServerChild.wait()
            logging.debug("Gdbserver stopped")

    def StopGDBServerRemotely(self):
        logging.debug("Stopping the remote gdbserver session")
        output, err = pexpect.run("gdb -batch -ex 'set pagination off' -ex 'target extended-remote localhost:{}' -ex 'monitor exit' -ex 'set confirm off' -ex quit -ex quit".format(
            self.local_port), withexitstatus=True)

        strOutput = output.decode("utf-8")
        if err != 0:
            raise GDBCommandException(strOutput)

        logging.debug("Remote gdbserver session closed")

    def GetContainerName(self):
        if not self.args.container:
            logging.debug("Finding out the container name since it isn't provided")

            output, err = pexpect.run("{kubectl} get pods {pod} -n {namespace} -o jsonpath='{{.spec.containers[*].name}}'".format(
                kubectl=self.args.kubectl_cmd,
                namespace=self.args.namespace,
                pod=self.args.pod), withexitstatus=True)
            if err != 0:
                raise KubectlError("Cannot get containers for {}/{} pod".format(self.args.namespace, self.args.pod))

            lines = output.decode("utf-8").splitlines(keepends=False)

            if len(lines) == 0:
                raise ParseError("Impossible: pod without containers")
            elif len(lines) == 1:
                self.args.container = lines[0].strip("'")
            else:
                raise ParseError("There is more than one container in pod. Please specify one of them: " + ", ".join(lines))

        logging.debug("The container name for debug is: " + self.args.container)

    def BuildStaticBinary(self, execName):
        logging.debug("Building '{}' static binary with docker")
        output, err = pexpect.run("{docker} build -t {execName}-static-{postfix} -f Dockerfile-{execName} .".format(
            docker=self.args.docker_cmd,
            execName=execName,
            postfix=os.getenv("USER")), withexitstatus=True, timeout=600)
        if err != 0:
            raise BuildStaticBinaryException("Cannot build static bin: " + output.decode("utf-8"))

        logging.debug("Creating a temporary container from built image")
        output, err = pexpect.run("{docker} create {execName}-static-{postfix}".format(
            docker=self.args.docker_cmd,
            execName=execName,
            postfix=os.getenv("USER")), withexitstatus=True)
        if err != 0:
            raise BuildStaticBinaryException("Cannot run docker create: " + output.decode("utf-8"))

        containerID = output.decode("utf-8").strip()
        logging.debug("New container ID: " + containerID)

        logging.debug("Copying the static binary from the container")
        cpOutput, cpErr = pexpect.run("{docker} cp {containerID}:/build/{execName} ./{execName}".format(
            docker=self.args.docker_cmd,
            containerID=containerID,
            execName=execName), withexitstatus=True)

        logging.debug("Remove the temporary docker container")
        output, err = pexpect.run("{docker} rm {containerID}".format(
            docker=self.args.docker_cmd,
            containerID=containerID), withexitstatus=True)
        if err != 0:
            raise BuildStaticBinaryException("Cannot run docker rm: " + output.decode("utf-8"))

        if cpErr != 0:
            raise BuildStaticBinaryException("Cannot run docker cp: " + cpOutput.decode("utf-8"))

    def SigIntHandler(self, sig, frame):
        self.StopDebug()

if __name__ == "__main__":
    # Prepare argument parser
    parser = argparse.ArgumentParser(description="A tool for helping to debug with gdb running kubernetes containers")

    parser.add_argument("-n", "--namespace", default="default", help="K8s namespace of the pod")
    parser.add_argument("pod", help="K8s pod for gdb debug")
    parser.add_argument("-c", "--container", required=False, help="container to debug in pod (if there are more than one containers present in pod then it should be provided")
    parser.add_argument("-p", "--pid", default=1, type=int, help="PID in container to attach the gdbserver")
    parser.add_argument("-l", "--local_port", required=False, type=int, help="local port to use the gdbserver (if not provided the a free one will be selected")
    parser.add_argument("-r", "--remote_port", default=2000, type=int, help="remote port in container to use the gdbserver")
    parser.add_argument("--kubectl_cmd", default="kubectl", help="path to kubectl executable")
    parser.add_argument("--docker_cmd", default="docker", help="path to docker executable")
    parser.add_argument("--log", dest="logLevel", choices=['DEBUG', "INFO", "ERROR"], default="INFO", help="Set the log level")

    # Parse the arguments
    args = parser.parse_args()

    logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=getattr(logging, args.logLevel))

    try:
        logging.debug("Evaluate that the 'kubectl' command is found")
        if not pexpect.which(args.kubectl_cmd):
            raise ExecutableNotFound(args.kubectl_cmd)

        logging.debug("Evaluate that the 'docker' command is found")
        if not pexpect.which(args.docker_cmd):
            raise ExecutableNotFound(args.docker_cmd)

        logging.debug("Evaluate that the 'gdb' command is found")
        if not pexpect.which("gdb"):
            raise ExecutableNotFound("gdb")

        k8sGDBServer = K8sGDBServer(args)

        logging.debug("Setting SIGINT handler")
        signal.signal(signal.SIGINT, k8sGDBServer.SigIntHandler)

        logging.debug("Initiate the gdbserver debug")
        k8sGDBServer.StartDebug()

        logging.info("Press Ctrl+C to close the gdbserver and portforward")

        signal.pause()
    except Exception as e:
        logging.error("Exception occurred", exc_info=True)