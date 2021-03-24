

"""
ModelServiceWorker is the worker that is started by the MMS front-end.
Communication message format: binary encoding
"""

# pylint: disable=redefined-builtin
# pylint: skip-file


import logging
import os
import platform
import socket
import sys
import tempfile
import time
import traceback
import torch.multiprocessing as mp
from multiprocessing import Process

from ts.arg_parser import ArgParser
from ts.model_service_worker import TorchModelServiceWorker
from ts.model_loader import ModelLoaderFactory
from ts.protocol.otf_message_handler import retrieve_msg, create_load_model_response, create_scale_model_response
from ts.service import emit_metrics

MAX_FAILURE_THRESHOLD = 5
SOCKET_ACCEPT_TIMEOUT = 30.0
DEBUG = False
BENCHMARK = os.getenv('TS_BENCHMARK')
BENCHMARK = BENCHMARK in ['True', 'true', 'TRUE']


class TorchModelServiceWorkerManager(object):
    """
    Backend worker to handle Model Server's python service code
    """
    def __init__(self, s_type=None, s_name=None, host_addr=None, port_num=None):
        self.sock_type = s_type
        if s_type == "unix":
            if s_name is None:
                raise ValueError("Wrong arguments passed. No socket name given.")
            self.sock_name, self.port = s_name, -1
            try:
                os.remove(s_name)
            except OSError as e :
                if os.path.exists(s_name):
                    raise RuntimeError("socket already in use: {}.".format(s_name)) from e

        elif s_type == "tcp":
            self.sock_name = host_addr if host_addr is not None else "127.0.0.1"
            if port_num is None:
                raise ValueError("Wrong arguments passed. No socket port given.")
            self.port = port_num
        else:
            raise ValueError("Incomplete data provided")

        logging.info("Listening on port: %s", s_name)
        socket_family = socket.AF_INET if s_type == "tcp" else socket.AF_UNIX
        self.sock = socket.socket(socket_family, socket.SOCK_STREAM)
        self.port_num = port_num
        self.workers = {}

    @staticmethod
    def load_model(load_model_request):
        """
        Expected command
        {
            "command" : "load", string
            "modelPath" : "/path/to/model/file", string
            "modelName" : "name", string
            "gpu" : None if CPU else gpu_id, int
            "handler" : service handler entry point if provided, string
            "batchSize" : batch size, int
        }

        :param load_model_request:
        :return:
        """
        try:
            model_dir = load_model_request["modelPath"].decode("utf-8")
            model_name = load_model_request["modelName"].decode("utf-8")
            handler = load_model_request["handler"].decode("utf-8") if load_model_request["handler"] else None
            batch_size = None
            if "batchSize" in load_model_request:
                batch_size = int(load_model_request["batchSize"])

            gpu = None
            if "gpu" in load_model_request:
                gpu = int(load_model_request["gpu"])

            model_service, model_service_args = None, None
            is_eager = False
            model_loader = ModelLoaderFactory.get_model_loader()
            service = model_loader.load(model_name, model_dir, handler, gpu, batch_size, init_service=False)
            if service.context.manifest:
                is_eager = 'modelFile' in service.context.manifest['model']
            if(is_eager):
                logging.info("Loading Eager Model")
                service = model_loader.load(model_name, model_dir, handler, gpu, batch_size, init_service=True)
                model_service, model_service_args = service, None
            else:
                logging.info("Delegating Initializing scripted Model Load to Child Process")
                model_service, model_service_args =  None, (model_name, model_dir, handler, gpu, batch_size)
            return model_service, model_service_args, "loaded model {}".format(model_name), 200


        except MemoryError:
            return None, None, "System out of memory", 507


    def scale_up(self, scale_up_request, service, service_args):
        try:
            sock_name = scale_up_request["sock_name"].decode("utf-8")
            sock_type = scale_up_request["sock_type"].decode("utf-8")
            host = scale_up_request["host"].decode("utf-8")
            port = scale_up_request["port"].decode("utf-8")
            fifo_path = scale_up_request["fifo_path"].decode("utf-8")

            worker = TorchModelServiceWorker(sock_type, sock_name, host, port, service, service_args, fifo_path)

            def create_fifo(file_name):
                os.remove(file_name) if os.path.exists(file_name) else None
                logging.info("Created file - " + file_name)
                open(file_name, "w").close()

            create_fifo(fifo_path + ".out")
            create_fifo(fifo_path + ".err")

            p = mp.Process(target=worker.run_server)
            p.start()
            worker_id = sock_name if sock_name else port
            self.workers[worker_id] = { "worker" : p, "fifo" : fifo_path }

            retry = 10
            while (retry > 0):
                time.sleep(1)
                with open(fifo_path + ".out", 'r') as f:
                    if("Torch worker started." in f.read()):
                        break
                with open(fifo_path + ".err", 'r') as f:
                    if("Torch worker started." in f.read()):
                        break
                retry = retry - 1

            if(retry == 0):
                raise Exception("Worker not spawned")

            return "scaled up", 200
        except:
            e = sys.exc_info()[0]
            traceback.print_exc()
            logging.info("Scale up error" + str(e))
            return "scale up failed", 500

    def scale_down(self, scale_down_request):
        try:
            port = scale_down_request["port"].decode("utf-8")
            worker = self.workers[port]["worker"]
            fifo_path = self.workers[port]["fifo"]
            worker.terminate()
            return "scaled down", 200
        except:
            e = sys.exc_info()[0]
            logging.info("Scale down error" + str(e))
            raise
            return "scale down failed", 500



    def handle_connection(self, cl_socket):
        """
        Handle socket connection.

        :param cl_socket:
        :return:
        """
        service = None
        service_args = None
        mp.set_start_method('spawn')
        while True:
            if BENCHMARK:
                pr.disable()
                pr.dump_stats('/tmp/tsPythonProfile.prof')
            cmd, msg = retrieve_msg(cl_socket)
            if BENCHMARK:
                pr.enable()
            elif cmd == b'L':
                logging.info("Received Load Model Request")
                service, service_args, result, code = self.load_model(msg)
                resp = bytearray()
                resp += create_load_model_response(code, result)
                cl_socket.send(resp)
                if code != 200:
                    raise RuntimeError("{} - {}".format(code, result))
            elif cmd == b'U':
                logging.info("Received Scale Up Request")
                result, code = self.scale_up(msg, service, service_args)
                resp = bytearray()
                resp += create_scale_model_response(code, result)
                cl_socket.send(resp)
                if code != 200:
                    raise RuntimeError("{} - {}".format(code, result))
            elif cmd == b'D':
                 logging.info("Received Scale Down Request" + str(msg))
                 result, code = self.scale_down(msg)
                 resp = bytearray()
                 code, result = 200, 'DONE'
                 resp += create_scale_model_response(code, result)
                 cl_socket.send(resp)
                 if code != 200:
                     raise RuntimeError("{} - {}".format(code, result))

            else:
                raise ValueError("Received unknown command: {}".format(cmd))

            if service is not None and service.context is not None and service.context.metrics is not None:
                emit_metrics(service.context.metrics.store)

    def run_server(self):
        """
        Run the backend worker process and listen on a socket
        :return:
        """
        if not DEBUG:
            self.sock.settimeout(SOCKET_ACCEPT_TIMEOUT)

        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        if self.sock_type == "unix":
            self.sock.bind(self.sock_name)
        else:
            self.sock.bind((self.sock_name, int(self.port)))

        self.sock.listen(1)
        logging.info("[PID]%d", os.getpid())
        logging.info("Torch worker manager started.")
        logging.info("Python runtime: %s", platform.python_version())

        while True:
            (cl_socket, _) = self.sock.accept()
            # workaround error(35, 'Resource temporarily unavailable') on OSX
            cl_socket.setblocking(True)

            logging.info("Connection accepted: %s.", cl_socket.getsockname())
            self.handle_connection(cl_socket)


if __name__ == "__main__":
    # Remove ts dir from python path to avoid module name conflict.
    ts_path = os.path.dirname(os.path.realpath(__file__))
    while ts_path in sys.path:
        sys.path.remove(ts_path)

    sock_type = None
    socket_name = None

    # noinspection PyBroadException
    try:
        logging.basicConfig(stream=sys.stdout, format="%(message)s", level=logging.INFO)
        args = ArgParser.model_service_worker_args().parse_args()
        socket_name = args.sock_name
        sock_type = args.sock_type
        host = args.host
        port = args.port

        if BENCHMARK:
            import cProfile
            pr = cProfile.Profile()
            pr.disable()
            pr.dump_stats('/tmp/tsPythonProfile.prof')

        worker = TorchModelServiceWorkerManager(sock_type, socket_name, host, port)
        worker.run_server()
        if BENCHMARK:
            pr.disable()
            pr.dump_stats('/tmp/tsPythonProfile.prof')

    except socket.timeout:
        logging.error("Backend worker did not receive connection in: %d", SOCKET_ACCEPT_TIMEOUT)
    except Exception:  # pylint: disable=broad-except
        logging.error("Backend worker process died.", exc_info=True)
    finally:
        if sock_type == 'unix' and os.path.exists(socket_name):
            os.remove(socket_name)

    sys.exit(1)