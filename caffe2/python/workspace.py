import contextlib
from google.protobuf.message import Message
from multiprocessing import Process
import os
import shutil
import socket
import tempfile

from caffe2.proto import caffe2_pb2
from caffe2.python import scope, utils
from ._import_c_extension import *  # noqa


def _GetFreeFlaskPort():
    """Get a free flask port."""
    # We will prefer to use 5000. If not, we will then pick a random port.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    result = sock.connect_ex(('127.0.0.1', 5000))
    if result == 0:
        return 5000
    else:
        s = socket.socket()
        s.bind(('', 0))
        port = s.getsockname()[1]
        s.close()
        # Race condition: between the interval we close the socket and actually
        # start a mint process, another process might have occupied the port. We
        # don't do much here as this is mostly for convenience in research
        # rather than 24x7 service.
        return port


def StartMint(root_folder=None, port=None):
    """Start a mint instance.

    TODO(Yangqing): this does not work well under ipython yet. According to
        https://github.com/ipython/ipython/issues/5862
    writing up some fix is a todo item.
    """
    from caffe2.python.mint import app
    if root_folder is None:
        # Get the root folder from the current workspace
        root_folder = RootFolder()
    if port is None:
        port = _GetFreeFlaskPort()
    process = Process(
        target=app.main,
        args=(
            ['-p', str(port), '-r', root_folder],
        )
    )
    process.start()
    print('Mint running at http://{}:{}'.format(socket.getfqdn(), port))
    return process


def StringfyProto(obj):
    """Stringfy a protocol buffer object.

  Inputs:
    obj: a protocol buffer object, or a Pycaffe2 object that has a Proto()
        function.
  Outputs:
    string: the output protobuf string.
  Raises:
    AttributeError: if the passed in object does not have the right attribute.
  """
    if type(obj) is str:
        return obj
    else:
        if isinstance(obj, Message):
            # First, see if this object is a protocol buffer, which we can
            # simply serialize with the SerializeToString() call.
            return obj.SerializeToString()
        elif hasattr(obj, 'Proto'):
            return obj.Proto().SerializeToString()


def ResetWorkspace(root_folder=None):
    if root_folder is None:
        # Reset the workspace, but keep the current root folder setting.
        return cc_ResetWorkspace(RootFolder())
    else:
        if not os.path.exists(root_folder):
            os.makedirs(root_folder)
        return cc_ResetWorkspace(root_folder)


def CreateNet(net, input_blobs=[]):
    for input_blob in input_blobs:
        CreateBlob(input_blob)
    return cc_CreateNet(StringfyProto(net))


def RunOperatorOnce(operator):
    return cc_RunOperatorOnce(StringfyProto(operator))


def RunOperatorsOnce(operators):
    for op in operators:
        success = RunOperatorOnce(op)
        if not success:
            return False
    return True


def RunNetOnce(net):
    return cc_RunNetOnce(StringfyProto(net))


def RunPlan(plan):
    return cc_RunPlan(StringfyProto(plan))


def FeedBlob(name, arr, device_option=None):
    """Feeds a blob into the workspace.

    Inputs:
      name: the name of the blob.
      arr: either a TensorProto object or a numpy array object to be fed into
          the workspace.
      device_option (optional): the device option to feed the data with.
    Returns:
      True or False, stating whether the feed is successful.
    """
    if type(arr) is caffe2_pb2.TensorProto:
        arr = utils.Caffe2TensorToNumpyArray(arr)
    if device_option is not None:
        return cc_FeedBlob(name, arr, StringfyProto(device_option))
    elif scope.DEVICESCOPE is not None:
        return cc_FeedBlob(name, arr, StringfyProto(scope.DEVICESCOPE))
    else:
        return cc_FeedBlob(name, arr)


class Model(object):
    def __init__(self, net, parameters, inputs, outputs, device_option=None):
        """Initializes a model.

        Inputs:
          net: a Caffe2 NetDef protocol buffer.
          parameters: a TensorProtos object containing the parameters to feed
              into the network.
          inputs: a list of strings specifying the input blob names.
          outputs: a list of strings specifying the output blob names.
          device_option (optional): the device option used to run the model. If
              not given, we will use the net's device option.
        """
        self._name = net.name
        self._inputs = inputs
        self._outputs = outputs
        if device_option:
            self._device_option = device_option.SerializeToString()
        else:
            self._device_option = net.device_option.SerializeToString()
        # For a caffe2 net, before we create it, it needs to have all the
        # parameter blobs ready. The construction is in two steps: feed in all
        # the parameters first, and then create the network object.
        for param in parameters.protos:
            print('Feeding parameter {}'.format(param.name))
            FeedBlob(param.name, param, net.device_option)
        if not CreateNet(net, inputs):
            raise RuntimeError("Error when creating the model.")

    def Run(self, input_arrs):
        """Runs the model with the given input.

        Inputs:
          input_arrs: an iterable of input arrays.
        Outputs:
          output_arrs: a list of output arrays.
        """
        if len(input_arrs) != len(self._inputs):
            raise RuntimeError("Incorrect number of inputs.")
        for i, input_arr in enumerate(input_arrs):
            FeedBlob(self._inputs[i], input_arr, self._device_option)
        if not RunNet(self._name):
            raise RuntimeError("Error in running the network.")
        return [FetchBlob(s) for s in self._outputs]


################################################################################
# Utilities for immediate mode
#
# Caffe2's immediate mode implements the following behavior: between the two
# function calls StartImmediate() and StopImmediate(), for any operator that is
# called through CreateOperator(), we will also run that operator in a workspace
# that is specific to the immediate mode. The user is explicitly expected to
# make sure that these ops have proper inputs and outputs, i.e. one should not
# run an op where an external input is not created or fed.
#
# Users can use FeedImmediate() and FetchImmediate() to interact with blobs
# in the immediate workspace.
#
# Once StopImmediate() is called, all contents in the immediate workspace is
# freed up so one can continue using normal runs.
#
# The immediate mode is solely for debugging purposes and support will be very
# sparse.
################################################################################

_immediate_mode = False
_immediate_workspace_name = "_CAFFE2_IMMEDIATE"
_immediate_root_folder = ''


def IsImmediate():
    return _immediate_mode


@contextlib.contextmanager
def WorkspaceGuard(workspace_name):
    current = CurrentWorkspace()
    SwitchWorkspace(workspace_name, True)
    yield
    SwitchWorkspace(current)


def StartImmediate(i_know=False):
    global _immediate_mode
    global _immediate_root_folder
    if IsImmediate():
        # already in immediate mode. We will kill the previous one
        # and start from fresh.
        StopImmediate()
    _immediate_mode = True
    with WorkspaceGuard(_immediate_workspace_name):
        _immediate_root_folder = tempfile.mkdtemp()
        ResetWorkspace(_immediate_root_folder)
    if i_know:
        # if the user doesn't want to see the warning message, sure...
        return
    print("""
    Enabling immediate mode in caffe2 python is an EXTREMELY EXPERIMENTAL
    feature and may very easily go wrong. This is because Caffe2 uses a
    declarative way of defining operators and models, which is essentially
    not meant to run things in an interactive way. Read the following carefully
    to make sure that you understand the caveats.

    (1) You need to make sure that the sequences of operators you create are
    actually runnable sequentially. For example, if you create an op that takes
    an input X, somewhere earlier you should have already created X.

    (2) Caffe2 immediate uses one single workspace, so if the set of operators
    you run are intended to be under different workspaces, they will not run.
    To create boundaries between such use cases, you can call FinishImmediate()
    and StartImmediate() manually to flush out everything no longer needed.

    (3) Underlying objects held by the immediate mode may interfere with your
    normal run. For example, if there is a leveldb that you opened in immediate
    mode and did not close, your main run will fail because leveldb does not
    support double opening. Immediate mode may also occupy a lot of memory esp.
    on GPUs. Call FinishImmediate() as soon as possible when you no longer
    need it.

    (4) Immediate is designed to be slow. Every immediate call implicitly
    creates a temp operator object, runs it, and destroys the operator. This
    slow-speed run is by design to discourage abuse. For most use cases other
    than debugging, do NOT turn on immediate mode.

    (5) If there is anything FATAL happening in the underlying C++ code, the
    immediate mode will immediately (pun intended) cause the runtime to crash.

    Thus you should use immediate mode with extra care. If you still would
    like to, have fun [https://xkcd.com/149/].
    """)


def StopImmediate():
    """Stops an immediate mode run."""
    # Phew, that was a dangerous ride.
    global _immediate_mode
    global _immediate_root_folder
    if not IsImmediate():
        return
    with WorkspaceGuard(_immediate_workspace_name):
        ResetWorkspace()
    shutil.rmtree(_immediate_root_folder)
    _immediate_root_folder = ''
    _immediate_mode = False


def ImmediateBlobs():
    with WorkspaceGuard(_immediate_workspace_name):
        return Blobs()


def RunOperatorImmediate(op):
    with WorkspaceGuard(_immediate_workspace_name):
        RunOperatorOnce(op)


def FetchImmediate(*args, **kwargs):
    with WorkspaceGuard(_immediate_workspace_name):
        return FetchBlob(*args, **kwargs)


def FeedImmediate(*args, **kwargs):
    with WorkspaceGuard(_immediate_workspace_name):
        return FeedBlob(*args, **kwargs)
