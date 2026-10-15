#!/usr/bin/env python3
"""Importa QCOW2 ou VMDK de URL ou arquivo local, em batch ou single."""

import argparse
import copy
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pty
import re
import select
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

BUCKET_NAME = "custom_image"
POLL_INTERVAL = 30
CAPACITY_TIMEOUT = 7200
FIELDS = {"source", "compartment_id", "image_name"}
WORK_TERMINAL = {"SUCCEEDED", "FAILED", "CANCELED", "CANCELLED"}
FINISHED = {"DONE", "FAILED"}
EXAMPLE = json.dumps({
    'images': [
        {'source': 'https://example.com/imagem-1.qcow2',
         'compartment_id': 'ocid1.compartment.oc1..substitua_pelo_ocid',
         'image_name': 'Minha imagem 1'},
        {'source': 'https://example.com/imagem-2.vmdk',
         'compartment_id': 'ocid1.compartment.oc1..substitua_pelo_ocid',
         'image_name': 'Minha imagem 2'},
    ]
}, ensure_ascii=False, indent=2) + '\n'


def now():
    return datetime.now(timezone.utc).isoformat()


class AuditLog:
    """Um evento JSON por linha, com flush imediato e escrita segura entre workers."""

    def __init__(self, directory):
        self.directory = Path(directory)
        self.lock = threading.Lock()
        self.calls = {}

    def __enter__(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
        descriptor, filename = tempfile.mkstemp(prefix=f'import-qcow2-{timestamp}-', suffix='.log', dir=self.directory)
        self.path = Path(filename)
        self.file = os.fdopen(descriptor, 'w', encoding='utf-8')
        return self

    def write(self, event, record=None, **details):
        with self.lock:
            entry = dict(time=now(), event=event)
            if record is not None:
                entry.update(image_name=record['image_name'], image_key=record['key'])
            entry.update(details)
            self.file.write(json.dumps(entry, ensure_ascii=False) + '\n')
            self.file.flush()

    def count(self, command):
        with self.lock:
            action = ' '.join(command[:4] if command[:3] == ('compute', 'image', 'import') else command[:3])
            self.calls[action] = self.calls.get(action, 0) + 1

    def __exit__(self, *unused):
        self.file.close()


class CommandError(RuntimeError):
    def __init__(self, command, stderr):
        super().__init__(f"Falha em {' '.join(command[:5])}: {stderr.strip()}")
        self.stderr = stderr
        try:
            self.details = json.loads(stderr[stderr.index('{'):])
        except (ValueError, TypeError):
            self.details = {}

    def capacity_rejected(self):
        return self.details.get('status') == 429 or (
            self.details.get('status') == 409 and any(
                word in self.details.get('message', '').lower()
                for word in ('concurren', 'limit', 'too many')))


class Stopped(RuntimeError):
    pass


def run_command(command):
    try:
        result = subprocess.run(command, check=True, text=True, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return result.stdout
    except subprocess.CalledProcessError as exc:
        raise CommandError(command, exc.stderr or str(exc)) from exc
    except OSError as exc:
        raise RuntimeError(f"Não foi possível executar {command[0]}: {exc}") from exc


def oci(*command):
    raw = run_command(['oci', *command, '--output', 'json', '--query', '@'])
    description = 'oci ' + ' '.join(command[:4])
    try:
        response = json.loads(raw) if raw.strip() else {}
    except ValueError as exc:
        raise RuntimeError(f'{description}: a OCI CLI retornou uma resposta que não é JSON válido.') from exc

    # A OCI CLI pode omitir totalmente o corpo quando uma listagem está vazia.
    # Não interpretar ausência de dados de um GET como ausência de recursos.
    action = command[:3]
    if action == ('os', 'object', 'list'):
        # object_list da OCI CLI renderiza a lista diretamente em data e inclui
        # prefixes no topo. Sem objetos, pode emitir apenas {"prefixes": []}.
        if response is None:
            response = {}
        if isinstance(response, list):
            response = {'data': response}
        if not isinstance(response, dict):
            raise RuntimeError(f'{description}: formato inesperado da listagem de objetos.')
        if 'data' in response:
            objects = response['data']
        elif 'objects' in response:
            objects = response
        elif set(response) <= {'prefixes', 'next-start-with'}:
            objects = []
        else:
            raise RuntimeError(f'{description}: listagem sem data, objects ou metadados reconhecidos.')
        if objects is None:
            objects = []
        elif isinstance(objects, dict) and 'objects' in objects:
            objects = objects['objects']
            if objects is None:
                objects = []
        if not isinstance(objects, list) or any(
                not isinstance(item, dict) or not isinstance(item.get('name'), str)
                for item in objects):
            raise RuntimeError(f'{description}: esperado uma lista de objetos com campo name.')
        return dict(response, data={'objects': objects})

    list_actions = {
        ('os', 'multipart', 'list'),
        ('compute', 'image', 'list'), ('work-requests', 'work-request', 'list'),
    }
    if action in list_actions:
        empty = []
        if response is None or response == {}:
            return {'data': empty}
        if isinstance(response, list):
            return {'data': response}
        if isinstance(response, dict) and 'data' in response and response['data'] is None:
            return dict(response, data=empty)

    data_actions = list_actions | {
        ('os', 'ns', 'get'), ('os', 'bucket', 'get'), ('os', 'bucket', 'create'),
        ('compute', 'image', 'get'), ('compute', 'image', 'import'),
        ('work-requests', 'work-request', 'get'),
    }
    if not isinstance(response, dict):
        raise RuntimeError(f'{description}: formato inesperado de resposta da OCI CLI; esperado um objeto JSON.')
    if action in data_actions and ('data' not in response or response['data'] is None):
        raise RuntimeError(f'{description}: resposta da OCI CLI sem o campo obrigatório "data". '
                           'Verifique a configuração e a saída desse comando no Cloud Shell.')
    return response


def is_remote(source):
    parsed = urlparse(source)
    return parsed.scheme in {'http', 'https'} and bool(parsed.netloc)


def detect_image_type(filename):
    # Identificação básica do formato; a OCI valida integridade e compatibilidade.
    with open(filename, 'rb') as source:
        header = source.read(8)
    if len(header) == 8 and header[:4] == b'QFI\xfb' and int.from_bytes(header[4:], 'big') in {2, 3}:
        return 'QCOW2'
    if len(header) == 8 and header[:4] == b'KDMV' and int.from_bytes(header[4:], 'little') in {1, 2, 3}:
        return 'VMDK'
    raise ValueError(f'Formato não suportado em {filename}: esperado QCOW2 ou VMDK monolítico. '
                     'VDI, RAW, descritores VMDK separados e outros formatos não são convertidos pelo script.')


def validate_images(images, base_directory=None):
    if not isinstance(images, list) or not images:
        raise ValueError("'images' deve ser uma lista não vazia.")
    seen = set()
    for index, image in enumerate(images, 1):
        if not isinstance(image, dict) or set(image) != FIELDS:
            raise ValueError(f"Imagem {index}: informe apenas source, compartment_id e image_name.")
        if any(not isinstance(value, str) or not value.strip() for value in image.values()):
            raise ValueError(f"Imagem {index}: todos os campos devem ser textos não vazios.")
        if any(any(ord(char) < 32 or ord(char) == 127 for char in value) for value in image.values()):
            raise ValueError(f"Imagem {index}: caracteres de controle não são permitidos.")
        image.update({key: value.strip() for key, value in image.items()})
        source = image['source']
        if '://' in source:
            if not is_remote(source):
                raise ValueError(f"Imagem {index}: URL HTTP/HTTPS inválida.")
        else:
            path = Path(source).expanduser()
            if not path.is_absolute():
                path = Path(base_directory or Path.cwd()) / path
            path = path.resolve()
            if not path.is_file():
                raise ValueError(f"Imagem {index}: arquivo local não encontrado: {path}")
            detect_image_type(path)
            image['source'] = str(path)
        if not image['compartment_id'].startswith('ocid1.compartment.'):
            raise ValueError(f"Imagem {index}: OCID de compartment inválido.")
        identity = tuple(image[key] for key in sorted(FIELDS))
        if identity in seen:
            raise ValueError(f"Imagem {index}: entrada duplicada no lote.")
        seen.add(identity)
    return images


def read_batch(filename):
    with open(filename, encoding='utf-8-sig') as source:
        try:
            document = json.load(source)
        except json.JSONDecodeError as exc:
            raise ValueError(f'JSON inválido na linha {exc.lineno}, coluna {exc.colno}: {exc.msg}') from exc
    if not isinstance(document, dict) or set(document) != {'images'}:
        raise ValueError("O JSON deve conter apenas a chave 'images'.")
    return validate_images(document['images'], Path(filename).resolve().parent)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.state-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as target:
            json.dump(value, target, ensure_ascii=False, indent=2)
            target.write('\n')
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def execution_lock(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + '.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError('Outro processo está usando este arquivo de estado.') from exc
        yield


def display_width(value):
    return sum(0 if unicodedata.combining(char) else
               2 if unicodedata.east_asian_width(char) in 'WF' else 1 for char in value)


def render_table(records):
    width = max([len('Image Name')] + [display_width(row['image_name']) for row in records])
    total = len(records)
    digits = max(2, len(str(total)))
    widths = [max(len('Count'), digits * 2 + 1), width, 8, 6, 7, 8]
    border = '+' + '+'.join('-' * (size + 2) for size in widths) + '+'

    def line(values):
        return '| ' + ' | '.join(value + ' ' * max(0, size - display_width(value))
                                 for value, size in zip(values, widths)) + ' |'

    def percent(value, size):
        if value is None:
            return '-' * size
        return f'{int(value):03d}%' if isinstance(value, (int, float)) else str(value)

    def cleanup_status(value):
        if isinstance(value, (int, float)):
            return 'OK' if value >= 100 else 'cleaning'
        return percent(value, widths[-1])

    lines = [border, line(['Count', 'Image Name', 'download', 'upload', 'convert', 'clean-up']), border]
    for index, row in enumerate(records, 1):
        count = f'{index:0{digits}d}/{total:0{digits}d}'
        lines.append(line([count, row['image_name']] + [percent(row.get(field), size)
                                                for field, size in zip(('download', 'upload', 'convert'), widths[2:5])] +
                          [cleanup_status(row.get('cleanup'))]))
    return '\n'.join(lines + [border])


def render_progress(records):
    succeeded = sum(row.get('status') == 'DONE' for row in records)
    failed = sum(row.get('status') in {'FAILED', 'TRANSFER_ERROR'} for row in records)
    processed = succeeded + failed
    summary = (f'Processados: {processed}/{len(records)} | Sucesso: {succeeded} | '
               f'Falhas: {failed} | Pendentes: {len(records) - processed}')
    return render_table(records) + '\n' + summary


def render_created_images(records):
    images = [row for row in records
              if row.get('status') in {'CONVERTED', 'DONE'} and row.get('image_id')]
    if not images:
        return ''
    rows = [['Image Name', 'Image OCID']] + [
        [row['image_name'], row['image_id']] for row in images]
    widths = [max(display_width(row[column]) for row in rows) for column in range(2)]
    border = '+' + '+'.join('-' * (size + 2) for size in widths) + '+'

    def line(values):
        return '| ' + ' | '.join(value + ' ' * (size - display_width(value))
                                 for value, size in zip(values, widths)) + ' |'

    return '\n'.join([border, line(rows[0]), border] +
                     [line(row) for row in rows[1:]] + [border])


class Dashboard:
    def __init__(self, engine):
        self.engine = engine
        self.stop = threading.Event()
        self.thread = None
        self.last = None

    def draw(self, final=False):
        with self.engine.lock:
            output = render_progress(self.engine.state['images'])
            if self.engine.message:
                output += '\n' + self.engine.message
        if final or output != self.last:
            if sys.stdout.isatty():
                sys.stdout.write('\033[H\033[J')
            print(output, flush=True)
            self.last = output

    def __enter__(self):
        if sys.stdout.isatty():
            sys.stdout.write('\033[?25l')
        self.draw()
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()
        return self

    def loop(self):
        while not self.stop.wait(1):
            self.draw()

    def __exit__(self, *unused):
        self.stop.set()
        self.thread.join()
        self.draw(final=True)
        if sys.stdout.isatty():
            sys.stdout.write('\033[?25h')
            sys.stdout.flush()


def download_metadata_path(filename):
    return Path(str(filename) + '.http.json')


def download_file(url, filename, progress, stop):
    filename = Path(filename)
    metadata_path = download_metadata_path(filename)
    try:
        previous = json.loads(metadata_path.read_text())
    except (OSError, ValueError):
        previous = {}
    if not isinstance(previous, dict):
        previous = {}
    etag = previous.get('etag')
    validator = etag if etag and not etag.startswith('W/') else previous.get('last_modified')
    offset = filename.stat().st_size if filename.is_file() else 0
    restart_reason = None
    if offset and (previous.get('url') != url or not validator):
        restart_reason = 'Arquivo parcial sem metadados/validador HTTP confiável.'
        offset = 0
    headers = {'User-Agent': 'oci-qcow2-import/1.0', 'Accept-Encoding': 'identity'}
    if offset:
        headers.update({'Range': f'bytes={offset}-', 'If-Range': validator})
    try:
        response = urlopen(Request(url, headers=headers), timeout=30)
    except HTTPError as exc:
        if exc.code != 416 or not offset:
            raise
        with exc:
            same_validator = (exc.headers.get('ETag') == validator if validator == etag
                              else exc.headers.get('Last-Modified') == validator)
            if (exc.headers.get('Content-Range') == f'bytes */{offset}' and
                    previous.get('total') == offset and same_validator):
                previous.update(completed=True, resumed_from_bytes=offset, restart_reason=None)
                atomic_json(metadata_path, previous)
                progress(100)
                return
        restart_reason = 'Servidor recusou o intervalo; reiniciando com uma resposta completa.'
        offset = 0
        response = urlopen(Request(url, headers={key: value for key, value in headers.items()
                                               if key not in {'Range', 'If-Range'}}), timeout=30)
    with response:
        status = getattr(response, 'status', None) or 200
        if response.headers.get('Content-Encoding', 'identity') != 'identity':
            raise RuntimeError('Servidor retornou codificação incompatível com retomada por bytes.')
        if status == 206:
            match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('Content-Range', ''))
            if not match:
                raise RuntimeError('Content-Range ausente ou inválido; download parcial preservado.')
            start, end, total = map(int, match.groups())
            if start != offset or end < start or end >= total:
                raise RuntimeError('Content-Range incompatível; download parcial preservado.')
            if offset and previous.get('total') not in (None, 0, total):
                raise RuntimeError('Tamanho da origem mudou; download parcial preservado.')
            length = response.headers.get('Content-Length')
            if length is not None and int(length) != end - start + 1:
                raise RuntimeError('Content-Length incompatível com Content-Range.')
            if offset:
                returned_validator = (response.headers.get('ETag') if validator == etag
                                      else response.headers.get('Last-Modified'))
                if returned_validator is not None and returned_validator != validator:
                    raise RuntimeError('Origem mudou durante a retomada; download parcial preservado.')
        elif status == 200:
            if offset:
                restart_reason = 'Servidor ignorou Range ou a origem mudou; resposta completa substituiu o parcial.'
            offset = 0
            total = int(response.headers.get('Content-Length') or 0)
        else:
            raise RuntimeError(f'Resposta HTTP inesperada para download: {status}.')
        metadata = dict(url=url, etag=response.headers.get('ETag') or (previous.get('etag') if offset else None),
                        last_modified=response.headers.get('Last-Modified') or
                        (previous.get('last_modified') if offset else None), total=total,
                        resumed_from_bytes=offset, restart_reason=restart_reason, completed=False)
        received = offset
        with open(filename, 'ab' if offset else 'wb') as target:
            atomic_json(metadata_path, metadata)
            progress(min(99, received * 100 / total) if total else 'RUN')
            while True:
                if stop.is_set():
                    raise Stopped('Transferência interrompida.')
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                target.write(chunk)
                received += len(chunk)
                if total and received > total:
                    raise RuntimeError('Download excedeu o tamanho informado pelo servidor.')
                progress(min(99, received * 100 / total) if total else 'RUN')
            if total and received != total:
                raise RuntimeError(f'Download incompleto: {received}/{total} bytes.')
        metadata['completed'] = True
        atomic_json(metadata_path, metadata)
    progress(100)


def upload_file(command, progress, multipart, stop):
    """PTY no stderr permite ler a barra real do OCI CLI sem poluir a tabela."""
    master, slave = pty.openpty()
    process = None
    tail = ''
    upload_id = None
    try:
        with tempfile.TemporaryFile() as stdout:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stdout,
                                       stderr=slave, start_new_session=True)
            os.close(slave)
            slave = None
            while True:
                if stop.is_set():
                    raise Stopped('Upload interrompido; multipart e arquivo local preservados para retomada.')
                if select.select([master], [], [], 0.2)[0]:
                    try:
                        chunk = os.read(master, 8192)
                    except OSError as exc:
                        if exc.errno != errno.EIO:
                            raise
                        break
                    if not chunk:
                        break
                    tail = (tail + chunk.decode('utf-8', errors='replace'))[-65536:]
                    matches = re.findall(r'(\d{1,3})%', tail)
                    if matches:
                        progress(min(99, int(matches[-1])))
                    match = re.search(r'Upload ID:[ \t]*([^\s\x1b]+)[\r\n]', tail)
                    if match and match.group(1) != upload_id:
                        upload_id = match.group(1)
                        multipart(upload_id)
                elif process.poll() is not None:
                    break
            if process.wait() != 0:
                raise CommandError(command, tail)
        progress(100)
    finally:
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            except ProcessLookupError:
                process.wait()
        os.close(master)
        if slave is not None:
            os.close(slave)


class Importer:
    def __init__(self, images, args):
        self.args = args
        self.images = images
        self.job = fingerprint(sorted(images, key=lambda row: tuple(row[key] for key in sorted(FIELDS))))
        self.workdir = f'qcow2-import/{self.job}/'
        self.path = Path(args.results_file or f'.oci-import/{self.job}/state.json').resolve()
        self.log_directory = Path.cwd().resolve()
        self.lock = threading.RLock()
        self.persist_lock = threading.Lock()
        self.stop = threading.Event()
        self.message = ''
        self.remote_ready = False
        self.retry_at = 0
        self.rejected_since = None
        self.retry_delay = args.poll_interval
        self.audit = None
        self.checkpoint_signature = None
        self.state = {
            'version': 3, 'job': self.job, 'workdir': self.workdir,
            'bucket_name': BUCKET_NAME, 'bucket_created': False, 'namespace': None,
            'images': [dict(image, key=fingerprint(image), status='PENDING',
                            object_name=f'{self.workdir}{fingerprint(image)}.disk',
                            image_type=None,
                            download=None if is_remote(image['source']) else 'N/A',
                            upload=None, convert=None, cleanup=None)
                       for image in images],
        }

    def storage(self, *command):
        return self.api('os', *command, '--namespace-name', self.state['namespace'],
                        '--bucket-name', BUCKET_NAME)

    def log(self, event, record=None, **details):
        if self.audit is not None:
            self.audit.write(event, record, **details)

    def api(self, *command):
        if self.audit is not None:
            self.audit.count(command)
        try:
            return oci(*command)
        except Exception as exc:
            event = 'OCI_RESOURCE_NOT_FOUND' if isinstance(exc, CommandError) and exc.details.get('status') == 404 else 'OCI_ERROR'
            self.log(event, command=' '.join(command[:3]), error=str(exc))
            raise

    def update(self, record, **values):
        with self.lock:
            record.update(values)

    def save(self):
        with self.persist_lock:
            with self.lock:
                self.state['updated_at'] = now()
                snapshot = copy.deepcopy(self.state)
            atomic_json(self.path, snapshot)
            if self.remote_ready:
                self.storage('object', 'put', '--name', self.workdir + 'state.json',
                             '--file', str(self.path), '--force')
            signature = (snapshot.get('bucket_created'), snapshot.get('storage_cleaned'),
                         tuple((row['status'], row.get('work_status'), row.get('work_request_id'),
                                row.get('multipart_upload_id')) for row in snapshot['images']))
            if signature != self.checkpoint_signature:
                self.log('STATE_SAVED', local=str(self.path), remote=self.remote_ready)
                self.checkpoint_signature = signature

    def load_state(self, state):
        if state.get('version') != 3 or state.get('job') != self.job or state.get('workdir') != self.workdir:
            raise ValueError('Estado incompatível com a entrada/workdir; use a mesma lista de imagens e diretório da execução anterior.')
        if {row['key'] for row in state['images']} != {fingerprint(image) for image in self.images}:
            raise ValueError('Imagens do estado não correspondem à entrada.')
        by_key = {row['key']: row for row in state['images']}
        for image in self.images:
            key = fingerprint(image)
            record = by_key[key]
            if any(record.get(field) != image[field] for field in FIELDS) or record.get('object_name') != f'{self.workdir}{key}.disk':
                raise ValueError('Estado contém imagem ou objeto incompatível com este workdir.')
            if record.get('image_type') not in {None, 'QCOW2', 'VMDK'}:
                raise ValueError('Tipo de imagem inválido no estado da execução.')
        state['images'] = [by_key[fingerprint(image)] for image in self.images]
        self.state = state

    def setup(self):
        if self.path.exists():
            self.load_state(json.loads(self.path.read_text(encoding='utf-8')))
            self.log('LOCAL_STATE_LOADED', path=str(self.path))
        self.log('NAMESPACE_LOOKUP')
        namespace = self.api('os', 'ns', 'get')['data']
        self.log('NAMESPACE_RESOLVED', namespace=namespace)
        if self.state['namespace'] not in (None, namespace):
            raise ValueError('O estado pertence a outra tenancy/namespace.')
        self.state['namespace'] = namespace
        if all(row['status'] in FINISHED for row in self.state['images']) and self.state.get('storage_cleaned'):
            self.log('EXECUTION_ALREADY_COMPLETED')
            return False
        self.log('BUCKET_LOOKUP', bucket=BUCKET_NAME)
        try:
            bucket = self.storage('bucket', 'get')['data']
            self.log('BUCKET_REUSED', bucket=BUCKET_NAME)
        except CommandError as exc:
            if exc.details.get('status') != 404:
                raise
            # Persistir intenção antes de criar; falha ambígua não autoriza excluir um bucket existente.
            self.log('BUCKET_CREATE_STARTED', bucket=BUCKET_NAME, compartment_id=self.images[0]['compartment_id'])
            bucket = self.api('os', 'bucket', 'create', '--namespace-name', namespace, '--name', BUCKET_NAME,
                              '--compartment-id', self.images[0]['compartment_id'])['data']
            self.log('BUCKET_CREATED', bucket=BUCKET_NAME)
            self.state['bucket_created'] = True
            self.state['bucket_id'] = bucket.get('id') or bucket.get('time-created')
            self.save()
        bucket_id = bucket.get('id') or bucket.get('time-created')
        if self.state.get('bucket_id') and bucket_id != self.state['bucket_id']:
            raise ValueError('O bucket foi substituído desde a execução anterior; verifique o estado antes de retomar.')
        with tempfile.TemporaryDirectory(dir=self.path.parent) as directory:
            remote = Path(directory) / 'state.json'
            self.log('REMOTE_STATE_LOOKUP', object_name=self.workdir + 'state.json')
            try:
                self.storage('object', 'get', '--name', self.workdir + 'state.json',
                             '--file', str(remote))
            except CommandError as exc:
                if exc.details.get('status') != 404:
                    raise
                self.log('REMOTE_STATE_NOT_FOUND')
            else:
                remote_state = json.loads(remote.read_text(encoding='utf-8'))
                if remote_state.get('job') != self.job or remote_state.get('workdir') != self.workdir:
                    raise ValueError('O workdir remoto pertence a outra entrada; verifique o manifesto antes de retomar.')
                if remote_state.get('updated_at', '') >= self.state.get('updated_at', ''):
                    self.load_state(remote_state)
                    self.log('REMOTE_STATE_LOADED')
                else:
                    self.log('REMOTE_STATE_OLDER', selected='local')
                if self.state['namespace'] != namespace:
                    raise ValueError('Manifesto remoto pertence a outro namespace.')
                if self.state.get('bucket_id') and self.state['bucket_id'] != bucket_id:
                    raise ValueError('O manifesto remoto pertence a outro bucket.')
        self.state['bucket_id'] = bucket_id
        self.remote_ready = True
        self.state['storage_cleaned'] = False
        self.reconcile()
        self.save()
        self.log('WORKDIR_READY', bucket=BUCKET_NAME, workdir=self.workdir)
        return True

    def reconcile(self):
        objects = self.storage('object', 'list', '--prefix', self.workdir, '--all')['data']['objects']
        present = {item['name'] for item in objects}
        multipart = self.storage('multipart', 'list', '--all')['data']
        self.log('RESUME_SCAN', objects=len(objects), multipart_uploads=len(multipart))
        for record in self.state['images']:
            if record['status'] not in FINISHED | {'CONVERTED', 'REJECTED'}:
                self.recover_conversion(record, include_available=record['object_name'] in present or
                                        record['status'] != 'PENDING')
            if record['status'] in FINISHED | {'CONVERTED', 'REJECTED'} or record.get('work_request_id'):
                self.log('IMAGE_RESUMED', record, status=record['status'], image_id=record.get('image_id'),
                         work_request_id=record.get('work_request_id'))
                continue
            if record['object_name'] in present:
                self.check_local_source(record)
                if record.get('image_type') not in {'QCOW2', 'VMDK'}:
                    raise ValueError('Objeto já enviado sem formato registrado; verifique o estado antes de importar.')
                self.update(record, status='READY', download=100 if is_remote(record['source']) else 'N/A', upload=100)
                record.pop('multipart_upload_id', None)
                self.remove_download(record)
                self.log('UPLOADED_OBJECT_REUSED', record, object_name=record['object_name'])
            else:
                uploads = [upload for upload in multipart if upload['object'] == record['object_name']]
                if len(uploads) > 1:
                    raise RuntimeError(f"Múltiplos multipart uploads para {record['image_name']}; reconcilie antes de retomar.")
                if uploads:
                    record['multipart_upload_id'] = uploads[0]['upload-id']
                else:
                    record.pop('multipart_upload_id', None)
                record['status'] = 'PENDING'
                record['upload'] = None
                self.log('IMAGE_QUEUED', record, multipart_upload_id=record.get('multipart_upload_id'))

    def recover_conversion(self, record, include_available=False):
        uncertain = record['status'] in {'SUBMITTING', 'UNKNOWN'}
        if record.get('work_request_id'):
            if not record.get('image_id'):
                request = self.api('work-requests', 'work-request', 'get',
                                   '--work-request-id', record['work_request_id'])['data']
                identifiers = {resource['identifier'] for resource in request.get('resources', [])
                               if resource.get('identifier', '').startswith('ocid1.image.')}
                if len(identifiers) != 1:
                    raise RuntimeError(f"Work request {record['work_request_id']} sem OCID de imagem inequívoco; "
                                       'nenhuma nova importação enviada.')
                self.update(record, image_id=identifiers.pop())
            self.update(record, status='CONVERTING')
            return True
        image = None
        if record.get('image_id'):
            image = self.api('compute', 'image', 'get', '--image-id', record['image_id'])['data']
            image = dict(image, id=record['image_id'])
        else:
            candidates = self.api('compute', 'image', 'list', '--compartment-id', record['compartment_id'],
                                  '--display-name', record['image_name'], '--all')['data']
            importing = [item for item in candidates if item.get('lifecycle-state') == 'IMPORTING']
            matches = [item for item in candidates
                       if (item.get('freeform-tags', {}).get('qcow2-import-key') or '').endswith(':' + record['key'])
                       and (item.get('lifecycle-state') == 'IMPORTING' or
                            (uncertain or include_available) and item.get('lifecycle-state') == 'AVAILABLE')]
            if len(importing) > 1 or len(matches) > 1:
                identifiers = ', '.join(item['id'] for item in importing or matches)
                self.log('IMPORT_RECOVERY_AMBIGUOUS', record, image_ids=identifiers)
                raise RuntimeError(f"Mais de uma imagem candidata para {record['image_name']}: {identifiers}. "
                                   'Nenhuma nova importação enviada; reconcilie os OCIDs no estado antes de retomar.')
            if matches:
                image = matches[0]
            elif importing:
                identifier = importing[0]['id']
                self.log('IMPORT_NAME_CONFLICT', record, image_id=identifier)
                raise RuntimeError(f"Imagem {record['image_name']} já está sendo importada ({identifier}), "
                                   'mas não foi possível confirmar a mesma origem. Nenhuma nova importação enviada.')
        if image is None:
            if uncertain:
                raise RuntimeError(f"Submissão incerta de {record['image_name']}; consulte a OCI e reconcilie o estado antes de reenviar.")
            return False
        self.update(record, image_id=image['id'], status='UNKNOWN', upload=100,
                    download=100 if is_remote(record['source']) else 'N/A')
        self.log('SUBMISSION_RECONCILE_STARTED', record, image_id=image['id'])
        self.save()
        requests = self.api('work-requests', 'work-request', 'list',
                            '--compartment-id', record['compartment_id'], '--resource-id', image['id'], '--all')['data']
        active = [item for item in requests if item.get('status') not in WORK_TERMINAL]
        selected = active if active else requests
        if len(selected) == 1:
            self.update(record, work_request_id=selected[0]['id'])
        elif image.get('lifecycle-state') != 'AVAILABLE':
            raise RuntimeError(f"Submissão incerta de {record['image_name']} ({image['id']}): "
                               'work request ausente ou ambíguo. Nenhuma nova importação enviada; tente retomar depois.')
        self.update(record, status='CONVERTED' if image.get('lifecycle-state') == 'AVAILABLE' else 'CONVERTING',
                    convert=100 if image.get('lifecycle-state') == 'AVAILABLE' else 0)
        self.log('SUBMISSION_RECONCILED', record, image_id=image['id'], work_request_id=record.get('work_request_id'))
        self.remove_download(record)
        self.save()
        return True

    def local_file(self, record):
        if not is_remote(record['source']):
            return Path(record['source'])
        return self.path.parent / (record['key'] + '.disk')

    def remove_download(self, record):
        # Apenas cópias criadas pelo script pertencem à limpeza automática.
        if not is_remote(record['source']):
            self.log('LOCAL_SOURCE_PRESERVED', record, path=record['source'])
            return
        local = self.local_file(record)
        if local.exists():
            local.unlink()
            self.log('LOCAL_DISK_REMOVED', record, path=str(local))
        metadata_path = download_metadata_path(local)
        if metadata_path.exists():
            metadata_path.unlink()
            self.log('DOWNLOAD_METADATA_REMOVED', record, path=str(metadata_path))

    def check_local_source(self, record):
        if is_remote(record['source']):
            return None
        stat = self.local_file(record).stat()
        current = dict(size=stat.st_size, mtime_ns=stat.st_mtime_ns)
        if record.get('source_stat') not in (None, current):
            raise ValueError('O arquivo local mudou desde a tentativa anterior; restaure o original ou use outro caminho.')
        return current

    def transfer_priority(self, record):
        if not is_remote(record['source']):
            return 0
        if record.get('download') == 100 and self.local_file(record).is_file():
            return 1
        return 2

    def download(self, record):
        filename = self.local_file(record)
        try:
            if self.stop.is_set():
                raise Stopped('Execução interrompida.')
            if record.get('multipart_upload_id'):
                self.abort_multipart(record)
            self.update(record, status='DOWNLOADING', download=0)
            self.log('DOWNLOAD_STARTED', record, source=record['source'], path=str(filename),
                     existing_bytes=filename.stat().st_size if filename.is_file() else 0)
            self.save()
            decision_logged = False

            def progress(value):
                nonlocal decision_logged
                if not decision_logged:
                    try:
                        metadata = json.loads(download_metadata_path(filename).read_text())
                    except (OSError, ValueError):
                        metadata = {}
                    if metadata.get('resumed_from_bytes'):
                        self.log('DOWNLOAD_RESUMED', record, offset_bytes=metadata['resumed_from_bytes'])
                    elif metadata.get('restart_reason'):
                        self.log('DOWNLOAD_RESTARTED', record, reason=metadata['restart_reason'])
                    decision_logged = True
                self.update(record, download=value)

            download_file(record['source'], filename, progress, self.stop)
            try:
                metadata = json.loads(download_metadata_path(filename).read_text())
            except (OSError, ValueError):
                metadata = {}
            self.log('DOWNLOAD_COMPLETED', record, bytes=filename.stat().st_size,
                     resumed_from_bytes=metadata.get('resumed_from_bytes', 0),
                     restart_reason=metadata.get('restart_reason'))
            self.update(record, status='WAITING_UPLOAD', download=100)
            self.save()
        except Stopped:
            self.log('TRANSFER_INTERRUPTED', record, status=record['status'])
            raise
        except Exception as exc:
            self.transfer_failed(record, exc, 'download')

    def upload(self, record):
        filename = self.local_file(record)
        remote = is_remote(record['source'])
        try:
            if self.stop.is_set():
                raise Stopped('Execução interrompida.')
            if not remote:
                self.update(record, status='PREPARING', download='N/A')
                current_stat = self.check_local_source(record)
                self.update(record, source_stat=current_stat)
                self.log('LOCAL_SOURCE_SELECTED', record, path=str(filename), bytes=current_stat['size'])
            else:
                if record.get('download') != 100 or not filename.is_file():
                    raise RuntimeError('Cópia baixada indisponível; execute novamente para retomar o download.')
                self.log('LOCAL_DISK_REUSED', record, path=str(filename), bytes=filename.stat().st_size)
            image_type = detect_image_type(filename)
            self.update(record, image_type=image_type)
            self.log('IMAGE_FORMAT_DETECTED', record, image_type=image_type)
            self.update(record, status='UPLOADING', upload=0)
            self.save()
            resume = record.get('multipart_upload_id')
            self.log('UPLOAD_RESUMED' if resume else 'UPLOAD_STARTED', record,
                     object_name=record['object_name'], multipart_upload_id=resume)
            command = ['oci', 'os', 'object', 'resume-put' if resume else 'put',
                       '--namespace-name', self.state['namespace'], '--bucket-name', BUCKET_NAME,
                       '--name', record['object_name'], '--file', str(filename)]
            command += ['--upload-id', resume] if resume else ['--force']
            command += ['--output', 'json', '--query', '@']

            def multipart(upload_id):
                self.update(record, multipart_upload_id=upload_id)
                self.log('MULTIPART_UPLOAD_REGISTERED', record, upload_id=upload_id)
                self.save()

            if self.audit is not None:
                self.audit.count(tuple(command[1:]))
            upload_file(command, lambda value: self.update(record, upload=value), multipart, self.stop)
            if not remote:
                try:
                    self.check_local_source(record)
                except ValueError:
                    self.storage('object', 'delete', '--object-name', record['object_name'], '--force')
                    self.log('UNSTABLE_SOURCE_UPLOAD_REMOVED', record, object_name=record['object_name'])
                    raise
            self.log('UPLOAD_COMPLETED', record, object_name=record['object_name'])
            self.remove_download(record)
            with self.lock:
                record.pop('multipart_upload_id', None)
                record.update(status='READY', download=100 if remote else 'N/A', upload=100)
            self.save()
        except Stopped:
            self.log('TRANSFER_INTERRUPTED', record, status=record['status'])
            raise
        except Exception as exc:
            self.transfer_failed(record, exc, 'upload')

    def transfer_failed(self, record, error, stage):
        self.update(record, status='TRANSFER_ERROR', error=str(error), **{stage: 'ERR'})
        self.log('TRANSFER_FAILED', record, stage=stage, error=str(error))
        self.save()

    def transfer(self, record):
        if self.transfer_priority(record) == 2:
            self.download(record)
        if record['status'] != 'TRANSFER_ERROR':
            self.upload(record)

    def abort_multipart(self, record):
        self.log('MULTIPART_ABORT_STARTED', record, upload_id=record['multipart_upload_id'])
        try:
            self.storage('multipart', 'abort', '--object-name', record['object_name'],
                         '--upload-id', record['multipart_upload_id'], '--force')
        except CommandError as exc:
            if exc.details.get('status') != 404:
                raise
        record.pop('multipart_upload_id', None)
        self.log('MULTIPART_ABORT_COMPLETED', record)
        self.save()

    def submit(self, record):
        if self.recover_conversion(record):
            return
        if record.get('image_type') not in {'QCOW2', 'VMDK'}:
            raise ValueError('Tipo da imagem não foi identificado antes da submissão.')
        self.update(record, status='SUBMITTING')
        self.log('IMPORT_SUBMIT_STARTED', record, compartment_id=record['compartment_id'],
                 object_name=record['object_name'], image_type=record['image_type'])
        self.save()
        try:
            response = self.api('compute', 'image', 'import', 'from-object',
                           '--compartment-id', record['compartment_id'], '--display-name', record['image_name'],
                           '--launch-mode', 'PARAVIRTUALIZED', '--source-image-type', record['image_type'],
                           '--bucket-name', BUCKET_NAME, '--name', record['object_name'],
                           '--namespace', self.state['namespace'], '--no-retry',
                           '--freeform-tags', json.dumps({'qcow2-import-key': self.job + ':' + record['key']}))
        except CommandError as exc:
            if exc.capacity_rejected():
                self.log('IMPORT_CAPACITY_REJECTED', record, error=str(exc))
                self.update(record, status='READY')
                self.rejected_since = self.rejected_since or time.monotonic()
                if time.monotonic() - self.rejected_since >= self.args.capacity_timeout:
                    self.update(record, status='REJECTED', convert='ERR', error=str(exc))
                    self.rejected_since = None
                    self.retry_delay = self.args.poll_interval
                    self.log('IMPORT_CAPACITY_TIMEOUT', record)
                else:
                    self.retry_at = time.monotonic() + self.retry_delay
                    self.log('IMPORT_RETRY_SCHEDULED', record, delay_seconds=self.retry_delay)
                    self.retry_delay = min(self.retry_delay * 2, 300)
                    self.message = 'OCI recusou por capacidade; transferências continuam durante a espera.'
                self.save()
                return
            if exc.details.get('status') in {400, 401, 403, 404, 405, 409, 422}:
                self.update(record, status='REJECTED', convert='ERR', error=str(exc))
                self.log('IMPORT_REJECTED', record, error=str(exc))
                self.save()
                return
            self.update(record, status='UNKNOWN', error=str(exc))
            self.log('IMPORT_RESULT_UNKNOWN', record, error=str(exc))
            self.save()
            raise
        except Exception as exc:
            self.update(record, status='UNKNOWN')
            self.log('IMPORT_RESULT_UNKNOWN', record, error=str(exc))
            self.save()
            raise
        self.update(record, image_id=response.get('data', {}).get('id'),
                    work_request_id=response.get('opc-work-request-id'), response=response)
        if not record.get('image_id') or not record.get('work_request_id'):
            self.update(record, status='UNKNOWN')
            self.log('IMPORT_IDS_MISSING', record, image_id=record.get('image_id'),
                     work_request_id=record.get('work_request_id'))
            self.save()
            raise RuntimeError('OCI respondeu sem image_id/work_request_id; resposta salva para reconciliação.')
        self.update(record, status='CONVERTING', convert=0)
        self.log('IMPORT_ACCEPTED', record, image_id=record['image_id'], work_request_id=record['work_request_id'])
        self.save()
        self.rejected_since = None
        self.retry_delay = self.args.poll_interval

    def poll(self):
        for record in self.state['images']:
            if record['status'] != 'CONVERTING':
                continue
            try:
                response = self.api('work-requests', 'work-request', 'get',
                               '--work-request-id', record['work_request_id'])['data']
            except CommandError as exc:
                if exc.details.get('status') in {429, 500, 502, 503, 504}:
                    self.message = 'Consulta à OCI indisponível; preservando vaga e tentando no próximo intervalo.'
                    self.log('CONVERSION_POLL_RETRY', record, delay_seconds=self.args.poll_interval)
                    continue
                raise
            status = response['status']
            if status != record.get('work_status'):
                self.log('CONVERSION_STATUS', record, status=status, work_request_id=record['work_request_id'])
            self.update(record, work_status=status,
                        convert=100 if status == 'SUCCEEDED' else
                        'ERR' if status in WORK_TERMINAL else response.get('percent-complete', 'RUN'))
            if status in WORK_TERMINAL:
                if status == 'SUCCEEDED':
                    image = self.api('compute', 'image', 'get', '--image-id', record['image_id'])['data']
                    if image['lifecycle-state'] != 'AVAILABLE':
                        self.save()
                        continue
                self.update(record, status='CONVERTED' if status == 'SUCCEEDED' else 'REJECTED')
                if status != 'SUCCEEDED':
                    self.update(record, error=f'Conversão terminou com status {status}.')
                self.log('CONVERSION_COMPLETED' if status == 'SUCCEEDED' else 'CONVERSION_FAILED', record,
                         image_id=record['image_id'], work_request_id=record['work_request_id'], status=status)
            self.save()

    def cleanup_objects(self):
        for record in self.state['images']:
            if record['status'] not in {'CONVERTED', 'REJECTED'}:
                continue
            self.update(record, cleanup=0)
            self.log('OBJECT_CLEANUP_STARTED', record, object_name=record['object_name'])
            try:
                self.storage('object', 'delete', '--object-name', record['object_name'], '--force')
            except CommandError as exc:
                if exc.details.get('status') != 404:
                    self.update(record, cleanup='ERR', cleanup_error=str(exc))
                    self.log('OBJECT_CLEANUP_FAILED', record, error=str(exc))
                    raise
                self.log('OBJECT_ALREADY_ABSENT', record)
            self.update(record, cleanup=100, status='DONE' if record['status'] == 'CONVERTED' else 'FAILED')
            self.log('OBJECT_CLEANUP_COMPLETED', record, object_name=record['object_name'])
            self.save()

    def finish_storage(self):
        # Preservar o workdir para retomar transferências com falha.
        if any(row['status'] == 'TRANSFER_ERROR' for row in self.state['images']):
            self.log('WORKDIR_PRESERVED', workdir=self.workdir, reason='transfer_error')
            return
        self.log('WORKDIR_CLEANUP_STARTED', workdir=self.workdir)
        self.storage('object', 'delete', '--object-name', self.workdir + 'state.json', '--force')
        self.log('WORKDIR_CLEANUP_COMPLETED', workdir=self.workdir)
        self.remote_ready = False
        if self.state['bucket_created']:
            contents = self.storage('object', 'list', '--all')['data']['objects']
            multipart = self.storage('multipart', 'list', '--all')['data']
            self.log('BUCKET_CONTENTS_CHECKED', objects=len(contents), multipart_uploads=len(multipart))
            if not contents and not multipart:
                # Nunca usar --empty: objetos de outras execuções não são removidos.
                self.log('BUCKET_DELETE_STARTED', bucket=BUCKET_NAME)
                self.storage('bucket', 'delete', '--force')
                self.state['bucket_deleted'] = True
                self.log('BUCKET_DELETED', bucket=BUCKET_NAME)
            else:
                self.message = 'Bucket criado pelo script preservado: contém objetos ou uploads de outra execução.'
                self.log('BUCKET_PRESERVED', bucket=BUCKET_NAME, reason='not_empty')
        else:
            self.log('BUCKET_PRESERVED', bucket=BUCKET_NAME, reason='preexisting')
        self.state['storage_cleaned'] = True
        self.save()

    def cleanup_local(self):
        """Limpar apenas esta execução concluída; preservar logs e outras execuções."""
        if not self.state.get('storage_cleaned') or any(row['status'] != 'DONE' for row in self.state['images']):
            self.log('LOCAL_WORKDIR_PRESERVED', path=str(self.path.parent), reason='resume_required')
            return
        self.log('LOCAL_CLEANUP_STARTED', path=str(self.path.parent))
        # Logs de versões anteriores ficavam dentro do workdir: resgatá-los antes da limpeza.
        legacy_logs = self.path.parent / 'logs'
        if legacy_logs.is_dir():
            for source in sorted(legacy_logs.glob('run-*.log')):
                descriptor, destination = tempfile.mkstemp(prefix=source.stem + '-', suffix='.log', dir=self.log_directory)
                try:
                    with os.fdopen(descriptor, 'wb') as target, source.open('rb') as original:
                        shutil.copyfileobj(original, target)
                        target.flush()
                        os.fsync(target.fileno())
                except BaseException:
                    Path(destination).unlink(missing_ok=True)
                    raise
                source.unlink()
                self.log('LEGACY_LOG_MOVED', source=str(source), destination=destination)
            self.remove_empty_directory(legacy_logs)
        for record in self.state['images']:
            self.remove_download(record)
        self.path.unlink(missing_ok=True)
        self.log('LOCAL_STATE_REMOVED', path=str(self.path))
        Path(str(self.path) + '.lock').unlink(missing_ok=True)
        self.log('LOCAL_LOCK_REMOVED')
        directory = self.path.parent
        self.remove_empty_directory(directory)
        if directory.parent.name == '.oci-import':
            self.remove_empty_directory(directory.parent)
        self.log('LOCAL_CLEANUP_COMPLETED')

    def remove_empty_directory(self, directory):
        try:
            directory.rmdir()
        except FileNotFoundError:
            return
        except OSError as exc:
            if exc.errno not in {errno.ENOTEMPTY, errno.EEXIST}:
                raise
            self.log('LOCAL_DIRECTORY_PRESERVED', path=str(directory), reason='not_empty')
        else:
            self.log('LOCAL_DIRECTORY_REMOVED', path=str(directory))

    def run(self):
        with AuditLog(self.log_directory) as audit:
            self.audit = audit
            started = time.monotonic()
            self.log('EXECUTION_STARTED', job=self.job, mode=self.args.mode, images=len(self.images),
                     max_in_progress=self.args.max_in_progress, transfer_workers=self.args.transfer_workers,
                     upload_workers=self.args.upload_workers,
                     state=str(self.path), bucket=BUCKET_NAME, workdir=self.workdir)
            try:
                result = self._run()
            except BaseException as exc:
                result = 130 if isinstance(exc, KeyboardInterrupt) else 1
                self.log('EXECUTION_INTERRUPTED' if result == 130 else 'EXECUTION_FAILED', error=str(exc))
                raise
            finally:
                for record in self.state['images']:
                    self.log('IMAGE_RESULT', record, status=record['status'], image_id=record.get('image_id'),
                             work_request_id=record.get('work_request_id'), error=record.get('error'),
                             source=record['source'], image_type=record.get('image_type'),
                             cleanup_error=record.get('cleanup_error'))
                self.log('EXECUTION_FINISHED', exit_code=result, duration_seconds=round(time.monotonic() - started, 2),
                         success=sum(row['status'] == 'DONE' for row in self.state['images']),
                         failed=sum(row['status'] in {'FAILED', 'TRANSFER_ERROR'} for row in self.state['images']),
                         pending=sum(row['status'] not in FINISHED | {'TRANSFER_ERROR'} for row in self.state['images']),
                         oci_calls=audit.calls)
                self.audit = None
                created_images = render_created_images(self.state['images'])
                if created_images:
                    print('\nImagens criadas:')
                    print(created_images)
                print(f'Log: {audit.path}')
            return result

    def _run(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with execution_lock(self.path):
            if not self.setup():
                self.message = 'Execução já concluída; nenhuma imagem reenviada.'
                with Dashboard(self):
                    pass
                self.cleanup_local()
                return 1 if any(row['status'] != 'DONE' for row in self.state['images']) else 0
            futures = []
            waiting_for_slot = False
            download_executor = ThreadPoolExecutor(max_workers=self.args.transfer_workers)
            upload_executor = ThreadPoolExecutor(max_workers=self.args.upload_workers)
            try:
                with Dashboard(self):
                    pending = sorted((record for record in self.state['images']
                                      if record['status'] == 'PENDING'), key=self.transfer_priority)
                    self.log('TRANSFER_QUEUE_PLANNED', download_workers=self.args.transfer_workers,
                             upload_workers=self.args.upload_workers,
                             image_keys=[record['key'] for record in pending])
                    for record in pending:
                        if self.transfer_priority(record) == 2:
                            futures.append(download_executor.submit(self.download, record))
                        else:
                            self.update(record, status='UPLOAD_QUEUED')
                            futures.append(upload_executor.submit(self.upload, record))
                    next_poll = 0
                    while True:
                        for future in futures:
                            if future.done():
                                future.result()
                        for record in self.state['images']:
                            if record['status'] == 'WAITING_UPLOAD':
                                self.update(record, status='UPLOAD_QUEUED')
                                futures.append(upload_executor.submit(self.upload, record))
                        if time.monotonic() >= next_poll:
                            self.poll()
                            next_poll = time.monotonic() + self.args.poll_interval
                        self.cleanup_objects()
                        active = sum(row['status'] == 'CONVERTING' for row in self.state['images'])
                        waiting = active >= self.args.max_in_progress and any(
                            row['status'] == 'READY' for row in self.state['images'])
                        if waiting != waiting_for_slot:
                            self.log('CONVERSION_SLOT_WAIT_STARTED' if waiting else 'CONVERSION_SLOT_WAIT_ENDED',
                                     active=active, limit=self.args.max_in_progress)
                            waiting_for_slot = waiting
                        for record in self.state['images']:
                            if active >= self.args.max_in_progress or time.monotonic() < self.retry_at:
                                break
                            if record['status'] == 'READY':
                                self.submit(record)
                                active += record['status'] == 'CONVERTING'
                        with self.lock:
                            statuses = [row['status'] for row in self.state['images']]
                        if all(future.done() for future in futures) and all(
                                status in FINISHED | {'TRANSFER_ERROR'} for status in statuses):
                            break
                        self.stop.wait(0.2)
                    self.finish_storage()
                    failures = sum(row['status'] != 'DONE' for row in self.state['images'])
                    self.message = f'Finalizado: {len(self.images) - failures} sucesso(s), {failures} falha(s).'
                    if failures:
                        self.message += f' Estado: {self.path}'
                    if self.state['bucket_created'] and not self.state.get('bucket_deleted'):
                        self.message += '\nBucket preservado para retomada ou por conter outros objetos/uploads.'
            finally:
                self.stop.set()
                download_executor.shutdown(wait=True, cancel_futures=True)
                upload_executor.shutdown(wait=True, cancel_futures=True)
                # Workers já pararam; não sobrescrever estado enquanto transferem.
                with self.lock:
                    atomic_json(self.path, self.state)
                self.log('FINAL_LOCAL_STATE_SAVED', path=str(self.path))
            if not failures:
                self.cleanup_local()
            return 1 if failures else 0


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError('Informe um número inteiro maior que zero.')
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.set_defaults(results_file=None, poll_interval=POLL_INTERVAL, capacity_timeout=CAPACITY_TIMEOUT)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--batch-file', metavar='ARQUIVO.json', help='Arquivo JSON com a lista de imagens do modo batch')
    modes.add_argument('--source', metavar='URL_OU_ARQUIVO', help='URL HTTP/HTTPS ou arquivo local QCOW2/VMDK no modo single')
    modes.add_argument('--create-example', metavar='ARQUIVO.json', help='Gerar JSON de exemplo e encerrar')
    modes.add_argument('--check-status', metavar='STATE.json', help='Consultar work requests sem importar ou limpar')
    parser.add_argument('--compartment-id', help='Compartment da imagem no modo single')
    parser.add_argument('--image-name', help='Nome da imagem no modo single')
    parser.add_argument('--max-in-progress', type=positive_int, default=3, help='Limite de conversões simultâneas (padrão operacional: 3)')
    parser.add_argument('--transfer-workers', type=positive_int, default=2,
                        help='Limite de downloads simultâneos (padrão: 2)')
    parser.add_argument('--upload-workers', type=positive_int, default=2,
                        help='Limite de uploads simultâneos, independente dos downloads (padrão: 2)')
    args = parser.parse_args(argv)
    if args.source:
        if not args.compartment_id or not args.image_name:
            parser.error('Single exige --source, --compartment-id e --image-name.')
        args.mode = 'single'
    elif args.batch_file:
        if args.compartment_id is not None or args.image_name is not None:
            parser.error('Batch exige --batch-file e não aceita parâmetros de imagem single.')
        args.mode = 'batch'
    elif args.compartment_id is not None or args.image_name is not None:
        parser.error('Geração de exemplo e consulta de status não usam parâmetros de importação.')
    try:
        if args.create_example:
            with open(args.create_example, 'x', encoding='utf-8') as target:
                target.write(EXAMPLE)
            print(f'Exemplo JSON criado: {args.create_example}. Substitua as origens e os OCIDs antes de importar.')
            return 0
        if args.check_status:
            state = json.loads(Path(args.check_status).read_text(encoding='utf-8'))
            for row in state['images']:
                if row.get('work_request_id'):
                    work = oci('work-requests', 'work-request', 'get', '--work-request-id', row['work_request_id'])['data']
                    row['work_status'] = work['status']
                    row['convert'] = 100 if work['status'] == 'SUCCEEDED' else 'ERR' if work['status'] in WORK_TERMINAL else work.get('percent-complete', 'RUN')
            print(render_progress(state['images']))
            return 1 if any(row['status'] in {'FAILED', 'TRANSFER_ERROR', 'UNKNOWN'} or
                            row.get('work_status') in WORK_TERMINAL - {'SUCCEEDED'} for row in state['images']) else 0
        images = read_batch(args.batch_file) if args.batch_file else validate_images([{
            'source': args.source, 'compartment_id': args.compartment_id, 'image_name': args.image_name}])
        return Importer(images, args).run()
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        print(f'[-] {exc}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('\nInterrompido. Execute novamente com a mesma entrada, estado e workdir para retomar.', file=sys.stderr)
        return 130


if __name__ == '__main__':
    sys.exit(main())
