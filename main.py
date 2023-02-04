import os
import sys
import copy
import json
import threading
import heapq
import traceback

if '--dont-upcast-attention' in sys.argv:
    print("disabling upcasting of attention")
    os.environ['ATTN_PRECISION'] = "fp16"

import torch

import nodes

def get_input_data(inputs, class_def, outputs={}, prompt={}, extra_data={}):
    valid_inputs = class_def.INPUT_TYPES()
    input_data_all = {}
    for x in inputs:
        input_data = inputs[x]
        if isinstance(input_data, list):
            input_unique_id = input_data[0]
            output_index = input_data[1]
            obj = outputs[input_unique_id][output_index]
            input_data_all[x] = obj
        else:
            if ("required" in valid_inputs and x in valid_inputs["required"]) or ("optional" in valid_inputs and x in valid_inputs["optional"]):
                input_data_all[x] = input_data

    if "hidden" in valid_inputs:
        h = valid_inputs["hidden"]
        for x in h:
            if h[x] == "PROMPT":
                input_data_all[x] = prompt
            if h[x] == "EXTRA_PNGINFO":
                if "extra_pnginfo" in extra_data:
                    input_data_all[x] = extra_data['extra_pnginfo']
    return input_data_all

def recursive_execute(prompt, outputs, current_item, extra_data={}):
    unique_id = current_item
    inputs = prompt[unique_id]['inputs']
    class_type = prompt[unique_id]['class_type']
    class_def = nodes.NODE_CLASS_MAPPINGS[class_type]
    if unique_id in outputs:
        return []

    executed = []

    for x in inputs:
        input_data = inputs[x]

        if isinstance(input_data, list):
            input_unique_id = input_data[0]
            output_index = input_data[1]
            if input_unique_id not in outputs:
                executed += recursive_execute(prompt, outputs, input_unique_id, extra_data)

    input_data_all = get_input_data(inputs, class_def, outputs, prompt, extra_data)
    obj = class_def()

    outputs[unique_id] = getattr(obj, obj.FUNCTION)(**input_data_all)
    return executed + [unique_id]

def recursive_will_execute(prompt, outputs, current_item):
    unique_id = current_item
    inputs = prompt[unique_id]['inputs']
    will_execute = []
    if unique_id in outputs:
        return []

    for x in inputs:
        input_data = inputs[x]
        if isinstance(input_data, list):
            input_unique_id = input_data[0]
            output_index = input_data[1]
            if input_unique_id not in outputs:
                will_execute += recursive_will_execute(prompt, outputs, input_unique_id)

    return will_execute + [unique_id]

def recursive_output_delete_if_changed(prompt, old_prompt, outputs, current_item):
    unique_id = current_item
    inputs = prompt[unique_id]['inputs']
    class_type = prompt[unique_id]['class_type']
    class_def = nodes.NODE_CLASS_MAPPINGS[class_type]

    is_changed_old = ''
    is_changed = ''
    if hasattr(class_def, 'IS_CHANGED'):
        if 'is_changed' not in prompt[unique_id]:
            if unique_id in old_prompt and 'is_changed' in old_prompt[unique_id]:
                is_changed_old = old_prompt[unique_id]['is_changed']
            input_data_all = get_input_data(inputs, class_def)
            is_changed = class_def.IS_CHANGED(**input_data_all)
            prompt[unique_id]['is_changed'] = is_changed
        else:
            is_changed = prompt[unique_id]['is_changed']

    if unique_id not in outputs:
        return True

    to_delete = False
    if is_changed != is_changed_old:
        to_delete = True
    elif unique_id not in old_prompt:
        to_delete = True
    elif inputs == old_prompt[unique_id]['inputs']:
        for x in inputs:
            input_data = inputs[x]

            if isinstance(input_data, list):
                input_unique_id = input_data[0]
                output_index = input_data[1]
                if input_unique_id in outputs:
                    to_delete = recursive_output_delete_if_changed(prompt, old_prompt, outputs, input_unique_id)
                else:
                    to_delete = True
                if to_delete:
                    break
    else:
        to_delete = True

    if to_delete:
        print("deleted", unique_id)
        d = outputs.pop(unique_id)
        del d
    return to_delete

class PromptExecutor:
    def __init__(self):
        self.outputs = {}
        self.old_prompt = {}

    def execute(self, prompt, extra_data={}):
        with torch.no_grad():
            for x in prompt:
                recursive_output_delete_if_changed(prompt, self.old_prompt, self.outputs, x)

            current_outputs = set(self.outputs.keys())
            executed = []
            try:
                to_execute = []
                for x in prompt:
                    class_ = nodes.NODE_CLASS_MAPPINGS[prompt[x]['class_type']]
                    if hasattr(class_, 'OUTPUT_NODE'):
                        to_execute += [(0, x)]

                while len(to_execute) > 0:
                    #always execute the output that depends on the least amount of unexecuted nodes first
                    to_execute = sorted(list(map(lambda a: (len(recursive_will_execute(prompt, self.outputs, a[-1])), a[-1]), to_execute)))
                    x = to_execute.pop(0)[-1]

                    class_ = nodes.NODE_CLASS_MAPPINGS[prompt[x]['class_type']]
                    if hasattr(class_, 'OUTPUT_NODE'):
                        if class_.OUTPUT_NODE == True:
                            valid = False
                            try:
                                m = validate_inputs(prompt, x)
                                valid = m[0]
                            except:
                                valid = False
                            if valid:
                                executed += recursive_execute(prompt, self.outputs, x, extra_data)

            except Exception as e:
                print(traceback.format_exc())
                to_delete = []
                for o in self.outputs:
                    if o not in current_outputs:
                        to_delete += [o]
                        if o in self.old_prompt:
                            d = self.old_prompt.pop(o)
                            del d
                for o in to_delete:
                    d = self.outputs.pop(o)
                    del d
            else:
                executed = set(executed)
                for x in executed:
                    self.old_prompt[x] = copy.deepcopy(prompt[x])

def validate_inputs(prompt, item):
    unique_id = item
    inputs = prompt[unique_id]['inputs']
    class_type = prompt[unique_id]['class_type']
    obj_class = nodes.NODE_CLASS_MAPPINGS[class_type]

    class_inputs = obj_class.INPUT_TYPES()
    required_inputs = class_inputs['required']
    for x in required_inputs:
        if x not in inputs:
            return (False, "Required input is missing. {}, {}".format(class_type, x))
        val = inputs[x]
        info = required_inputs[x]
        type_input = info[0]
        if isinstance(val, list):
            if len(val) != 2:
                return (False, "Bad Input. {}, {}".format(class_type, x))
            o_id = val[0]
            o_class_type = prompt[o_id]['class_type']
            r = nodes.NODE_CLASS_MAPPINGS[o_class_type].RETURN_TYPES
            if r[val[1]] != type_input:
                return (False, "Return type mismatch. {}, {}".format(class_type, x))
            r = validate_inputs(prompt, o_id)
            if r[0] == False:
                return r
        else:
            if type_input == "INT":
                val = int(val)
                inputs[x] = val
            if type_input == "FLOAT":
                val = float(val)
                inputs[x] = val
            if type_input == "STRING":
                val = str(val)
                inputs[x] = val

            if len(info) > 1:
                if "min" in info[1] and val < info[1]["min"]:
                    return (False, "Value smaller than min. {}, {}".format(class_type, x))
                if "max" in info[1] and val > info[1]["max"]:
                    return (False, "Value bigger than max. {}, {}".format(class_type, x))

            if isinstance(type_input, list):
                if val not in type_input:
                    return (False, "Value not in list. {}, {}: {} not in {}".format(class_type, x, val, type_input))
    return (True, "")

def validate_prompt(prompt):
    outputs = set()
    for x in prompt:
        class_ = nodes.NODE_CLASS_MAPPINGS[prompt[x]['class_type']]
        if hasattr(class_, 'OUTPUT_NODE') and class_.OUTPUT_NODE == True:
            outputs.add(x)

    if len(outputs) == 0:
        return (False, "Prompt has no outputs")

    good_outputs = set()
    errors = []
    for o in outputs:
        valid = False
        reason = ""
        try:
            m = validate_inputs(prompt, o)
            valid = m[0]
            reason = m[1]
        except:
            valid = False
            reason = "Parsing error"

        if valid == True:
            good_outputs.add(x)
        else:
            print("Failed to validate prompt for output {} {}".format(o, reason))
            print("output will be ignored")
            errors += [(o, reason)]

    if len(good_outputs) == 0:
        errors_list = "\n".join(map(lambda a: "{}".format(a[1]), errors))
        return (False, "Prompt has no properly connected outputs\n {}".format(errors_list))

    return (True, "")

def prompt_worker(q):
    e = PromptExecutor()
    while True:
        item, item_id = q.get()
        e.execute(item[-2], item[-1])
        q.task_done(item_id)

class PromptQueue:
    def __init__(self):
        self.mutex = threading.RLock()
        self.not_empty = threading.Condition(self.mutex)
        self.task_counter = 0
        self.queue = []
        self.currently_running = {}

    def put(self, item):
        with self.mutex:
            heapq.heappush(self.queue, item)
            self.not_empty.notify()

    def get(self):
        with self.not_empty:
            while len(self.queue) == 0:
                self.not_empty.wait()
            item = heapq.heappop(self.queue)
            i = self.task_counter
            self.currently_running[i] = copy.deepcopy(item)
            self.task_counter += 1
            return (item, i)

    def task_done(self, item_id):
        with self.mutex:
            self.currently_running.pop(item_id)

    def get_current_queue(self):
        with self.mutex:
            out = []
            for x in self.currently_running.values():
                out += [x]
            return (out, copy.deepcopy(self.queue))

    def get_tasks_remaining(self):
        with self.mutex:
            return len(self.queue) + len(self.currently_running)

    def wipe_queue(self):
        with self.mutex:
            self.queue = []

    def delete_queue_item(self, function):
        with self.mutex:
            for x in range(len(self.queue)):
                if function(self.queue[x]):
                    if len(self.queue) == 1:
                        self.wipe_queue()
                    else:
                        self.queue.pop(x)
                        heapq.heapify(self.queue)
                    return True
        return False

from http.server import BaseHTTPRequestHandler, HTTPServer

class PromptServer(BaseHTTPRequestHandler):
    def _set_headers(self, code=200, ct='text/html'):
        self.send_response(code)
        self.send_header('Content-type', ct)
        self.end_headers()
    def log_message(self, format, *args):
        pass
    def do_GET(self):
        if self.path == "/prompt":
            self._set_headers(ct='application/json')
            prompt_info = {}
            exec_info = {}
            exec_info['queue_remaining'] = self.server.prompt_queue.get_tasks_remaining()
            prompt_info['exec_info'] = exec_info
            self.wfile.write(json.dumps(prompt_info).encode('utf-8'))
        elif self.path == "/queue":
            self._set_headers(ct='application/json')
            queue_info = {}
            current_queue = self.server.prompt_queue.get_current_queue()
            queue_info['queue_running'] = current_queue[0]
            queue_info['queue_pending'] = current_queue[1]
            self.wfile.write(json.dumps(queue_info).encode('utf-8'))
        elif self.path == "/object_info":
            self._set_headers(ct='application/json')
            out = {}
            for x in nodes.NODE_CLASS_MAPPINGS:
                obj_class = nodes.NODE_CLASS_MAPPINGS[x]
                info = {}
                info['input'] = obj_class.INPUT_TYPES()
                info['output'] = obj_class.RETURN_TYPES
                info['name'] = x #TODO
                info['description'] = ''
                info['category'] = 'sd'
                if hasattr(obj_class, 'CATEGORY'):
                    info['category'] = obj_class.CATEGORY
                out[x] = info
            self.wfile.write(json.dumps(out).encode('utf-8'))
        elif self.path[1:] in os.listdir(self.server.server_dir):
            if self.path[1:].endswith('.css'):
                self._set_headers(ct='text/css')
            elif self.path[1:].endswith('.js'):
                self._set_headers(ct='text/javascript')
            else:
                self._set_headers()
            with open(os.path.join(self.server.server_dir, self.path[1:]), "rb") as f:
                self.wfile.write(f.read())
        else:
            self._set_headers()
            with open(os.path.join(self.server.server_dir, "index.html"), "rb") as f:
                self.wfile.write(f.read())

    def do_HEAD(self):
        self._set_headers()

    def do_POST(self):
        resp_code = 200
        out_string = ""
        if self.path == "/prompt":
            print("got prompt")
            data_string = self.rfile.read(int(self.headers['Content-Length']))
            json_data = json.loads(data_string)
            if "number" in json_data:
                number = float(json_data['number'])
            else:
                number = self.server.number
                if "front" in json_data:
                    if json_data['front']:
                        number = -number

                self.server.number += 1
            if "prompt" in json_data:
                prompt = json_data["prompt"]
                valid = validate_prompt(prompt)
                extra_data = {}
                if "extra_data" in json_data:
                    extra_data = json_data["extra_data"]
                if valid[0]:
                    self.server.prompt_queue.put((number, id(prompt), prompt, extra_data))
                else:
                    resp_code = 400
                    out_string = valid[1]
                    print("invalid prompt:", valid[1])
        elif self.path == "/queue":
            data_string = self.rfile.read(int(self.headers['Content-Length']))
            json_data = json.loads(data_string)
            if "clear" in json_data:
                if json_data["clear"]:
                    self.server.prompt_queue.wipe_queue()
            if "delete" in json_data:
                to_delete = json_data['delete']
                for id_to_delete in to_delete:
                    delete_func = lambda a: a[1] == int(id_to_delete)
                    self.server.prompt_queue.delete_queue_item(delete_func)

        self._set_headers(code=resp_code)
        self.end_headers()
        self.wfile.write(out_string.encode('utf8'))
        return


def run(prompt_queue, address='', port=8188):
    server_address = (address, port)
    httpd = HTTPServer(server_address, PromptServer)
    httpd.server_dir = os.path.join(os.path.dirname(os.path.realpath(__file__)), "webshit")
    httpd.prompt_queue = prompt_queue
    httpd.number = 0
    if server_address[0] == '':
        addr = '0.0.0.0'
    else:
        addr = server_address[0]
    print("Starting server\n")
    print("To see the GUI go to: http://{}:{}".format(addr, server_address[1]))
    httpd.serve_forever()


if __name__ == "__main__":
    q = PromptQueue()
    threading.Thread(target=prompt_worker, daemon=True, args=(q,)).start()
    if '--listen' in sys.argv:
        address = '0.0.0.0'
    else:
        address = '127.0.0.1'
    run(q, address=address, port=8188)


