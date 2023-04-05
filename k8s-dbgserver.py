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

class DbgCommandException(Exception):
    def __init__(self, message):
        super().__init__("Debugger command error: " + message)


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

class K8sDbgServer():
    def __init__(self, args):
        self.args = args
        self.portForwardChild = None
        self.dbgServerCmd = ""
        self.dbgServerChild = None

        self.version = GetKubernetesVersion(args.kubectl_cmd)

        self.local_port = ""
        if args.local_port:
            self.local_port = args.local_port

        self.use_dlv = False
        if args.golang:
            self.use_dlv = args.golang

    def StartDebug(self):
        self.GetContainerName()

        if pkg_resources.parse_version(self.version) >= pkg_resources.parse_version(KUBERNETES_EPHEMERAL_CONTAINER_VER):
            self.PrepareWithEphemeralContainer()
        else:
            if not self.IsExecutableInContainerImage("tar"):
                if not self.IsExecutableInContainerImage("tee"):
                    logging.critical("Cannot move debug server into the container")
                    exit(-1)
                else:
                    self.TryToAddTarExecutable()

            self.PrepareWithKubectlCP()

        self.StartPortForward()
        try:
            self.StartDebugServer()
        except:
            self.StopDebug()
            raise

        logging.info("Port fordwarded debugger is listening on localhost:" + str(self.local_port))

    def StopDebug(self):
        logging.info("Stopping debug")
        self.StopDebugServer()
        self.StopPortForward()

    def CleanupPrevDebuggerServerSession(self):
        logging.info("Trying to cleanup previous debugger session in the container")
        self.GetContainerName()
        self.StopDebugServerRemotely()

    def GenerateCoreFile(self):
        logging.info("Generating core file")
        if self.use_dlv:
            output, err = pexpect.run("echo \"dump /tmp/{corefile}\" | dlv connect localhost:{local_port}".format(
                local_port=self.local_port,
                corefile=self.args.gcore), withexitstatus=True)

            output2, err2 = pexpect.run("{kubectl} cp {namespace}/{pod}:/tmp/{corefile} {corefile} -c {container}".format(
                kubectl=self.args.kubectl_cmd,
                namespace=self.args.namespace,
                pod=self.args.pod,
                container=self.args.container,
                corefile=self.args.gcore), withexitstatus=True)
            if err2 != 0:
                raise KubectlError("Cannot cp from the container: " + output2.decode("utf-8"))

            output3, err3 = pexpect.run("{kubectl} exec -n {namespace} {pod} -c {container} -- rm /tmp/{corefile}".format(
                kubectl=self.args.kubectl_cmd,
                namespace=self.args.namespace,
                pod=self.args.pod,
                container=self.args.container,
                corefile=self.args.gcore), withexitstatus=True)
            if err3 != 0:
                raise KubectlError("Cannot remove temporary file from the container: " + output3.decode("utf-8"))
        else:
            output, err = pexpect.run("gdb -batch -ex 'set pagination off' -ex 'target extended-remote localhost:{local_port}' -ex 'gcore {corefile}' -ex 'disconnect' -ex 'set confirm off' -ex quit -ex quit".format(
                local_port=self.local_port,
                corefile=self.args.gcore), withexitstatus=True)

        strOutput = output.decode("utf-8")
        if err != 0:
            raise DbgCommandException(strOutput)

        if self.args.i_want_to_see_debugger_output:
            logging.debug("debugger command output:\n{}".format(strOutput))

        logging.debug("Core file generation finished")

    def PrepareWithEphemeralContainer(self):
        logging.info("Ephemeral containers are supported, so using 'kubectl debug' for gdbserver")

        self.dbgServerCmd = "{kubectl} debug -n {namespace} {pod} --image=albertdupre/gdbserver:latest --target={container} -i -- sh -c 'sleep 5 ; gdbserver --attach localhost:{port} {pid}'".format(
            kubectl=self.args.kubectl_cmd,
            namespace=self.args.namespace,
            pod=self.args.pod,
            port=self.args.remote_port,
            pid=self.args.pid,
            container=self.args.container)

    def PrepareWithKubectlCP(self):
        debugger = 'gdbserver'
        if self.use_dlv:
            debugger = 'dlv'

        logging.info("Using the kubectl cp for setting up '{}'".format(debugger))

        if not self.IsExecutableInContainerImage(debugger):
            logging.debug("'{}' is not present on the container".format(debugger))

            self.BuildStaticBinary(debugger)

            logging.debug("Copying the '{}' binary to the container".format(debugger))
            output, err = pexpect.run("{kubectl} cp ./{debugger} {namespace}/{pod}:/tmp/{debugger} -c {container}".format(
                kubectl=self.args.kubectl_cmd,
                namespace=self.args.namespace,
                pod=self.args.pod,
                container=self.args.container,
                debugger=debugger), withexitstatus=True)
            if err != 0:
                raise KubectlError("Cannot cp into the container: " + output.decode("utf-8"))

        if self.use_dlv:
            self.dbgServerCmd = "{kubectl} exec -n {namespace} {pod} -c {container} -- /tmp/dlv --headless --accept-multiclient --only-same-user=false --api-version=2 --listen localhost:{port} attach {pid}".format(
                kubectl=self.args.kubectl_cmd,
                namespace=self.args.namespace,
                pod=self.args.pod,
                container=self.args.container,
                port=self.args.remote_port,
                pid=self.args.pid)
        else:
            self.dbgServerCmd = "{kubectl} exec -n {namespace} {pod} -c {container} -- /tmp/gdbserver --attach localhost:{port} {pid}".format(
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
            logging.info("{} is not present in container on PATH, trying it in the /tmp".format(execName))

            output, err = pexpect.run("{kubectl} exec -n {namespace} {pod} -c {container} -- /tmp/{execName} --help".format(
                kubectl=self.args.kubectl_cmd,
                namespace=self.args.namespace,
                pod=self.args.pod,
                container=self.args.container,
                execName=execName), withexitstatus=True)

            if err != 0:
                logging.info("{} is not present in container in the /tmp".format(execName))
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
        logging.debug("The local debugger port is " + self.local_port)

    def StopPortForward(self):
        logging.debug("Sending SIGINT for portforward")
        self.portForwardChild.sendintr()
        self.portForwardChild.wait()
        logging.debug("Portforward stopped")

    def StartDebugServer(self):
        logging.info("Starting debugger")
        self.dbgServerChild = pexpect.spawn(self.dbgServerCmd)

        index = -1
        if self.use_dlv:
            index = self.dbgServerChild.expect([".*API server listening at: ", ".*bind: address already in use.", pexpect.EOF, pexpect.TIMEOUT])
        else:
            index = self.dbgServerChild.expect([".*Listening on port ", ".*Can't bind address: Address in use.", pexpect.EOF, pexpect.TIMEOUT])
        if index == 0:
            logging.debug("The remote debugger port is " + str(self.args.remote_port))
        elif index == 1:
            logging.error("The port is occupied")
            raise DbgCommandException("Address in use")
        else:
            # TODO write some output about the failure
            logging.error("Some error occurred")
            raise DbgCommandException("Some error occurred")

    def StopDebugServer(self):
        logging.info("Stopping debugger")
        self.StopDebugServerRemotely()

        if self.dbgServerChild.isalive():
            logging.debug("Sending SIGINT to local debugger session")
            self.dbgServerChild.sendintr()
            self.dbgServerChild.wait()
            logging.debug("Debugger stopped")

    def StopDebugServerRemotely(self):
        logging.debug("Stopping the remote debugger session")

        output, err = pexpect.run("{kubectl} exec -n {namespace} {pod} -c {container} -- sh -c 'kill -INT `ps aux | grep -Ew \"gdbserver|dlv\" | grep -v grep | tr -s \" \" | cut -d \" \" -f 2`'".format(
            kubectl=self.args.kubectl_cmd,
            namespace=self.args.namespace,
            pod=self.args.pod,
            container=self.args.container), withexitstatus=True)
        if err != 0:
            raise KubectlError("Error during killing debugger: " + output.decode("utf-8"))
        logging.debug("Remote debugger session closed")

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
        logging.debug("Building '{execName}' static binary with docker".format(execName=execName))
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
    parser = argparse.ArgumentParser(description="A tool for helping to debug with gdb or dlv running kubernetes containers")

    parser.add_argument("-n", "--namespace", default="default", help="K8s namespace of the pod")
    parser.add_argument("pod", help="K8s pod for debug")
    parser.add_argument("-c", "--container", required=False, help="container to debug in pod (if there are more than one containers present in pod then it should be provided")
    parser.add_argument("-p", "--pid", default=1, type=int, help="PID in container to attach the debug server")
    parser.add_argument("-l", "--local_port", required=False, type=int, help="local port to use the debug server (if not provided the a free one will be selected")
    parser.add_argument("-r", "--remote_port", default=2000, type=int, help="remote port in container to use the debug server")
    parser.add_argument("--kubectl_cmd", default="kubectl", help="path to kubectl executable")
    parser.add_argument("--docker_cmd", default="docker", help="path to docker executable")
    parser.add_argument("--log", dest="logLevel", choices=['DEBUG', "INFO", "ERROR"], default="INFO", help="Set the log level")
    parser.add_argument("--i_want_to_see_debugger_output",  default=False, action="store_true", help="See the debugger command output in logs")
    parser.add_argument("--cleanup_prev_dbgserver", default=False, action="store_true", help="try do a cleanup for previous debug server session")
    parser.add_argument("--gcore", default=None, type=str, help="create core file at given path")
    parser.add_argument("--golang", default=False, action="store_true", help="run dlv server for golang debug")

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

        k8sDbgServer = K8sDbgServer(args)

        if args.cleanup_prev_dbgserver:
            k8sDbgServer.CleanupPrevDebuggerServerSession()
        else:
            if args.gcore is None:
                logging.debug("Setting SIGINT handler")
                signal.signal(signal.SIGINT, k8sDbgServer.SigIntHandler)

            logging.debug("Initiate the remote debugger")
            k8sDbgServer.StartDebug()

            if args.gcore is None:
                logging.info("Press Ctrl+C to close the debugger and portforward")
                signal.pause()
            else:
                k8sDbgServer.GenerateCoreFile()
                k8sDbgServer.StopDebug()

    except Exception as e:
        logging.error("Exception occurred", exc_info=True)