import argparse
import json
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
    output, err = pexpect.run(kubectl_cmd + " version --output=json", withexitstatus=True)
    if err != 0:
        raise KubectlError("Cannot get kubernetes version")

    j = json.loads(output)

    if "serverVersion" in j and "gitVersion" in j["serverVersion"]:
        return j["serverVersion"]["gitVersion"]

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

        print("Port fordwarded gdbserver is listening on localhost:" + str(self.local_port))

    def StopDebug(self):
        self.StopGDBServer()
        self.StopPortForward()

    def PrepareWithEphemeralContainer(self):
        self.gdbServerCmd = "{kubectl} debug -n {namespace} {pod} --image=albertdupre/gdbserver:latest --target={pod} -i -- sh -c 'sleep 5 ; gdbserver --attach localhost:{port} {pid}'".format(
            kubectl=self.args.kubectl_cmd,
            namespace=self.args.namespace,
            pod=self.args.pod,
            port=self.args.remote_port,
            pid=self.args.pid)

    def PrepareWithKubectlCP(self):
        if not self.IsExecutableInContainerImage('gdbserver'):
            self.BuildStaticBinary("gdbserver")

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

        output, err = pexpect.run("{kubectl} cp ./gdbserver {namespace}/{pod}:/bin/gdbserver -c {container}".format(
            kubectl=self.args.kubectl_cmd,
            namespace=self.args.namespace,
            pod=self.args.pod,
            container=self.args.container), withexitstatus=True)

    def IsExecutableInContainerImage(self, execName):
        output, err = pexpect.run("{kubectl} exec -n {namespace} {pod} -c {container} -- {execName} --help".format(
            kubectl=self.args.kubectl_cmd,
            namespace=self.args.namespace,
            pod=self.args.pod,
            container=self.args.container,
            execName=execName), withexitstatus=True)

        return err == 0

    def TryToAddTarExecutable(self):
        if not self.IsExecutableInContainerImage('tar') or True:
            self.BuildStaticBinary("tar")

            output, err = pexpect.run("sh -c 'cat ./tar | {kubectl} exec -i -n {namespace} {pod} -c {container} -- tee /bin/tar'".format(
                kubectl=self.args.kubectl_cmd,
                namespace=self.args.namespace,
                pod=self.args.pod,
                container=self.args.container), withexitstatus=True)
            if err != 0:
                raise KubectlError("Error during sending tar executable to remote host: " + output.decode("utf-8"))

            output, err = pexpect.run("{kubectl} exec -n {namespace} {pod} -c {container} -- chmod +x /bin/tar".format(
                kubectl=self.args.kubectl_cmd,
                namespace=self.args.namespace,
                pod=self.args.pod,
                container=self.args.container), withexitstatus=True)
            if err != 0:
                raise KubectlError("Error during chmod of tar: " + output.decode("utf-8"))

    def StartPortForward(self):
        self.portForwardChild = pexpect.spawn("{kubectl} port-forward -n {namespace} pod/{pod} {local_port}:{remote_port}".format(
            kubectl=self.args.kubectl_cmd,
            namespace=self.args.namespace,
            pod=self.args.pod,
            local_port=self.local_port,
            remote_port=self.args.remote_port))

        self.portForwardChild.expect("Forwarding from .* ->")

        self.local_port = self.portForwardChild.after.decode("utf-8").splitlines()[0].split(":")[1].split(" -> ")[0]

    def StopPortForward(self):
        self.portForwardChild.sendintr()
        self.portForwardChild.wait()

    def StartGDBServer(self):
        self.gdbServerChild = pexpect.spawn(self.gdbServerCmd)

        self.gdbServerChild.expect(".*Listening on port ")

    def StopGDBServer(self):
        self.StopGDBServerRemotely()

        if self.gdbServerChild.isalive():
            self.gdbServerChild.sendintr()
            self.gdbServerChild.wait()

    def StopGDBServerRemotely(self):
        output, err = pexpect.run("gdb -ex 'set pagination off' -ex 'target extended-remote localhost:{}' -ex 'monitor exit' -ex 'set confirm off' -ex quit -ex quit".format(
            self.local_port), withexitstatus=True)
        if err != 0:
            raise GDBCommandException(output.decode("utf-8"))

    def GetContainerName(self):
        if not self.args.container:
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

    def BuildStaticBinary(self, execName):
        output, err = pexpect.run("{docker} build -t {execName}-static-{postfix} -f Dockerfile-{execName} .".format(
            docker=self.args.docker_cmd,
            execName=execName,
            postfix=os.getenv("USER")), withexitstatus=True, timeout=600)
        if err != 0:
            raise BuildStaticBinaryException("Cannot build static bin: " + output.decode("utf-8"))

        output, err = pexpect.run("{docker} create {execName}-static-{postfix}".format(
            docker=self.args.docker_cmd,
            execName=execName,
            postfix=os.getenv("USER")), withexitstatus=True)
        if err != 0:
            raise BuildStaticBinaryException("Cannot run docker create: " + output.decode("utf-8"))

        containerID = output.decode("utf-8").strip()

        cpOutput, cpErr = pexpect.run("{docker} cp {containerID}:/build/{execName} ./{execName}".format(
            docker=self.args.docker_cmd,
            containerID=containerID,
            execName=execName), withexitstatus=True)

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

    # Parse the arguments
    args = parser.parse_args()

    # Evaluate that the "kubectl" command is found
    if not pexpect.which(args.kubectl_cmd):
        raise ExecutableNotFound(args.kubectl_cmd)

    # Evaluate that the "docker" command is found
    if not pexpect.which(args.docker_cmd):
        raise ExecutableNotFound(args.docker_cmd)

    # Evaluate that the "gdb" command is found
    if not pexpect.which("gdb"):
        raise ExecutableNotFound("gdb")

    k8sGDBServer = K8sGDBServer(args)

    signal.signal(signal.SIGINT, k8sGDBServer.SigIntHandler)

    k8sGDBServer.StartDebug()

    print("Press Ctrl+C to close the gdbserver and portforward")

    signal.pause()