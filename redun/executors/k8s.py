import pdb
import datetime
import json
import logging
import os
import pickle
import re
import subprocess
import threading
import time
import uuid
from collections import OrderedDict, defaultdict
from functools import lru_cache
from itertools import islice
from shlex import quote
from tempfile import mkstemp
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple, cast
from urllib.error import URLError
from urllib.request import urlopen

from redun.executors import k8s_utils, s3_utils, aws_utils
from redun.executors.base import Executor, register_executor
from redun.file import File
from redun.job_array import AWS_ARRAY_VAR, JobArrayer
from redun.scheduler import Job, Scheduler, Traceback
from redun.scripting import ScriptError, get_task_command
from redun.task import Task
from redun.utils import get_import_paths, pickle_dump

class boto3:
    class Session:
        pass

ARRAY_JOB_SUFFIX = "array"
DOCKER_INSPECT_ERROR = "CannotInspectContainerError: Could not transition to inspecting"
BATCH_JOB_TIMEOUT_ERROR = "Job attempt duration exceeded timeout"


SUCCEEDED = 'SUCCEEDED'
FAILED = 'FAILED'

def is_array_job_name(job_name: str) -> bool:
    return job_name.endswith(f"-{ARRAY_JOB_SUFFIX}")


def k8s_submit(
    command: List[str],
    queue: str,
    image: str,
    job_def_name: Optional[str] = None,
    job_def_suffix: str = "-jd",
    job_name: str = "k8s-job",
    array_size: int = 0,
    memory: int = 4,
    vcpus: int = 1,
    gpus: int = 0,
    retries: int = 1,
    role: Optional[str] = None,
    privileged: bool = False,
    autocreate_job: bool = True,
    timeout: Optional[int] = None,
    batch_tags: Optional[Dict[str, str]] = None,
    propagate_tags: bool = True,
) -> Dict[str, Any]:
    print("====== k8s submit")
    api_instance = k8s_utils.get_k8s_batch_client()
    k8s_job = k8s_utils.create_job_object(job_name, image, command)
    api_response = k8s_utils.create_job(api_instance, k8s_job)
    print("Job created. status='%s'" % str(api_response.status))

    return api_response


def is_ec2_instance() -> bool:
    """
    Returns True if this process is running on an EC2 instance.

    We use the presence of a link-local address as a sign we are on an EC2 instance.
    """
    try:
        resp = urlopen("http://169.254.169.254/latest/meta-data/", timeout=1)
        return resp.status == 200
    except URLError:
        return False


def run_docker(
    command: List[str],
    image: str,
    array_index: int = -1,
    volumes: Optional[Iterable[Tuple[str, str]]] = None,
    interactive: bool = True,
    cleanup: bool = False,
) -> str:
    """
    volumes: a list of ('host', 'container') path pairs for volume mouting.
    """
    # Add AWS credentials to environment for docker command.
    env = dict(os.environ)
    if not is_ec2_instance():
        session = boto3.Session()
        creds = session.get_credentials().get_frozen_credentials()
        cred_map = {
            "AWS_ACCESS_KEY_ID": creds.access_key,
            "AWS_SECRET_ACCESS_KEY": creds.secret_key,
            "AWS_SESSION_TOKEN": creds.token,
        }
        defined = {k: v for k, v in cred_map.items() if v}
        env.update(defined)

    common_args = []
    if cleanup:
        common_args.append("--rm")

    # Environment args.
    common_args.extend(
        ["-e", "AWS_ACCESS_KEY_ID", "-e", "AWS_SECRET_ACCESS_KEY", "-e", "AWS_SESSION_TOKEN"]
    )

    # Add array index environment variable if running an array job
    if array_index >= 0:
        env.update({AWS_ARRAY_VAR: str(array_index)})
        common_args.extend(["-e", AWS_ARRAY_VAR])

    # Volume mounting args.
    if not volumes:
        volumes = []
    for host, container in volumes:
        common_args.extend(["-v", f"{host}:{container}"])

    common_args.append(image)
    common_args.extend(command)

    if interactive:
        # Adding this flag is necessary to prevent docker from hijacking the terminal and modifying
        # the tty settings. One of the modifications it makes when it hijacks the terminal is to
        # change the behavior of line endings which means output(like from logging) will be
        # malformed until the docker container exits and the hijacked connection is closed which
        # resets the tty settings.
        env["NORAW"] = "true"

        # Run Docker interactively.
        fd, cidfile = mkstemp()
        os.close(fd)
        os.remove(cidfile)

        docker_command = ["docker", "run", "-it", "--cidfile", cidfile] + common_args
        subprocess.check_call(docker_command, env=env)
        with open(cidfile) as infile:
            container_id = infile.read().strip()
        os.remove(cidfile)
    else:
        # Run Docker in the background.
        docker_command = ["docker", "run", "-d"] + common_args
        container_id = subprocess.check_output(docker_command, env=env).strip().decode("utf8")

    return container_id


def get_k8s_job_name(prefix: str, job_hash: str, array: bool = False) -> str:
    """
    Return a K8S Job name by either job or job hash.
    """
    return "{}-{}{}".format(prefix, job_hash, f"-{ARRAY_JOB_SUFFIX}" if array else "")


def get_hash_from_job_name(job_name: str) -> Optional[str]:
    """
    Returns the job/task eval_hash that corresponds with a particular job name
    on K8S.
    """
    # Remove array job suffix, if present.
    array_suffix = "-" + ARRAY_JOB_SUFFIX
    if job_name.endswith(array_suffix):
        job_name = job_name[: -len(array_suffix)]

    # It's possible we found jobs that are unrelated to the this work based off the job_name_prefix
    # matching when fetching in get_jobs. These jobs will not have hashes so we can ignore them.
    # For a concrete example of this, see:
    #
    #   https://insitro.atlassian.net/browse/DE-2632
    #
    # where a headnode job is running but has no hash so we don't want to interact with that job
    # here. If we don't find a match, consider this a case of the above where we matched unrelated
    # jobs and return None to let callers know this is the case.
    match = re.match(".*-(?P<hash>[^-]+)", job_name)
    if match:
        return match["hash"]

    return None


def get_k8s_job_options(job_options: dict) -> dict:
    """
    Returns K8S-specific job options from general job options.
    """
    keys = [
        "vcpus",
        "gpus",
        "memory",
        "role",
        "retries",
        "privileged",
        "job_def_name",
        "autocreate_job",
        "timeout",
        "batch_tags",
    ]
    return {key: job_options[key] for key in keys if key in job_options}


def get_docker_job_options(job_options: dict) -> dict:
    """
    Returns Docker-specific job options from general job options.
    """
    keys = ["volumes", "interactive"]
    return {key: job_options[key] for key in keys if key in job_options}


def submit_task(
    image: str,
    queue: str,
    s3_scratch_prefix: str,
    job: Job,
    a_task: Task,
    args: Tuple = (),
    kwargs: Dict[str, Any] = {},
    job_options: dict = {},
    array_uuid: Optional[str] = None,
    array_size: int = 0,
    debug: bool = False,
    code_file: Optional[File] = None,
) -> Dict[str, Any]:
    """
    Submit a redun Task to K8S or Docker (debug=True).
    """
    print("======= SUBMIT TASK")
    if array_size:
        # Output_path will contain a pickled list of actual output paths, etc.
        # Want files that won't get clobbered when jobs actually run
        assert array_uuid
        input_path = aws_utils.get_array_scratch_file(
            s3_scratch_prefix, array_uuid, s3_utils.S3_SCRATCH_INPUT
        )
        output_path = aws_utils.get_array_scratch_file(
            s3_scratch_prefix, array_uuid, s3_utils.S3_SCRATCH_OUTPUT
        )
        error_path = aws_utils.get_array_scratch_file(
            s3_scratch_prefix, array_uuid, s3_utils.S3_SCRATCH_ERROR
        )
    else:
        input_path = None
        output_path = None
        error_path = None
        input_path = aws_utils.get_job_scratch_file(
            s3_scratch_prefix, job, s3_utils.S3_SCRATCH_INPUT
        )
        output_path = aws_utils.get_job_scratch_file(
            s3_scratch_prefix, job, s3_utils.S3_SCRATCH_OUTPUT
        )
        error_path = aws_utils.get_job_scratch_file(
            s3_scratch_prefix, job, s3_utils.S3_SCRATCH_ERROR
        )

        # Serialize arguments to input file.
        # Array jobs set this up earlier, in `_submit_array_job`
        input_file = File(input_path)
        with input_file.open("wb") as out:
            pickle_dump([args, kwargs], out)

    # Determine additional python import paths.
    import_args = []
    base_path = os.getcwd()
    for abs_path in get_import_paths():
        # Use relative paths so that they work inside the docker container.
        rel_path = os.path.relpath(abs_path, base_path)
        import_args.append("--import-path")
        import_args.append(rel_path)

    # Build job command.
    code_arg = ["--code", code_file.path] if code_file else []
    array_arg = ["--array-job"] if array_size else []
    cache_arg = [] if job_options.get("cache", True) else ["--no-cache"]
    command = (
        [
            aws_utils.REDUN_PROG,
            "--check-version",
            aws_utils.REDUN_REQUIRED_VERSION,
            "oneshot",
            a_task.load_module,
        ]
        + import_args
        + code_arg
        + array_arg
        + cache_arg
        + ["--input", input_path, "--output", output_path, "--error", error_path, a_task.fullname]
    )

    if not debug:
        if array_uuid:
            job_hash = array_uuid
        else:
            assert job.eval_hash
            job_hash = job.eval_hash

        # Submit to K8S
        job_name = get_k8s_job_name(
            job_options.get("job_name_prefix", "k8s-job"), job_hash, array=bool(array_size)
        )

        result = k8s_submit(
            command,
            queue,
            image=image,
            job_name=job_name,
            job_def_suffix="-redun-jd",
            array_size=array_size,
            **get_k8s_job_options(job_options),
        )
    else:
        # Submit to local Docker.
        # This loop only runs if array_size > 0
        result = {"jobId": [], "redun_job_id": []}
        for i in range(array_size):
            container_id = run_docker(
                command, image=image, array_index=i, **get_docker_job_options(job_options)
            )
            result["jobId"].append(container_id)
            result["redun_job_id"].append(job.id)

        # Otherwise, submit one non-array job
        if not array_size:
            container_id = run_docker(command, image=image, **get_docker_job_options(job_options))
            result = {"jobId": container_id, "redun_job_id": job.id}
    return result


def submit_command(
    image: str,
    queue: str,
    s3_scratch_prefix: str,
    job: Job,
    command: str,
    job_options: dict = {},
    debug: bool = False,
) -> dict:
    """
    Submit a shell command to K8S or Docker (debug=True).
    """
    print("====== submit command")
    input_path = aws_utils.get_job_scratch_file(s3_scratch_prefix, job, s3_utils.S3_SCRATCH_INPUT)
    output_path = aws_utils.get_job_scratch_file(
        s3_scratch_prefix, job, s3_utils.S3_SCRATCH_OUTPUT
    )
    error_path = aws_utils.get_job_scratch_file(s3_scratch_prefix, job, s3_utils.S3_SCRATCH_ERROR)
    status_path = aws_utils.get_job_scratch_file(
        s3_scratch_prefix, job, s3_utils.S3_SCRATCH_STATUS
    )

    # Serialize arguments to input file.
    input_file = File(input_path)
    input_file.write(command)
    assert input_file.exists()

    # Build job command.
    shell_command = [
        "bash",
        "-c",
        "-o",
        "pipefail",
        """
aws s3 cp {input_path} .task_command
chmod +x .task_command
(
  ./.task_command \
  2> >(tee .task_error >&2) | tee .task_output
) && (
    aws s3 cp .task_output {output_path}
    aws s3 cp .task_error {error_path}
    echo ok | aws s3 cp - {status_path}
) || (
    [ -f .task_output ] && aws s3 cp .task_output {output_path}
    [ -f .task_error ] && aws s3 cp .task_error {error_path}
    echo fail | aws s3 cp - {status_path}
    {exit_command}
)
""".format(
            input_path=quote(input_path),
            output_path=quote(output_path),
            error_path=quote(error_path),
            status_path=quote(status_path),
            exit_command="exit 1" if not debug else "",
        ),
    ]

    if not debug:
        # Submit to K8S.
        assert job.eval_hash
        job_name = get_batch_job_name(
            job_options.get("job_name_prefix", "k8s-job"), job.eval_hash
        )

        # Submit to K8S.
        return k8s_submit(
            shell_command,
            queue,
            image=image,
            job_name=job_name,
            job_def_suffix="-redun-jd",
            **get_batch_job_options(job_options),
        )
    else:
        # Submit to local Docker.
        container_id = run_docker(
            shell_command, image=image, **get_docker_job_options(job_options)
        )
        return {"jobId": container_id, "redun_job_id": job.id}


def parse_task_error(
    s3_scratch_prefix: str, job: Job, k8s_job_metadata: Optional[dict] = None
) -> Tuple[Exception, "Traceback"]:
    """
    Parse task error from s3 scratch path.
    """
    assert job.task

    error_path = aws_utils.get_job_scratch_file(s3_scratch_prefix, job, s3_utils.S3_SCRATCH_ERROR)
    error_file = File(error_path)

    if not job.task.script:
        # Normal Tasks (non-script) store errors as Pickled exception, traceback tuples.
        if error_file.exists():
            error, error_traceback = pickle.loads(cast(bytes, error_file.read("rb")))
        else:
            if k8s_job_metadata:
                try:
                    status_reason = k8s_job_metadata["attempts"][-1]["statusReason"]
                except (KeyError, IndexError):
                    status_reason = ""
            else:
                status_reason = ""

            if status_reason == BATCH_JOB_TIMEOUT_ERROR:
                error = K8SBatchJobTimeoutError(BATCH_JOB_TIMEOUT_ERROR)
            else:
                error = K8SBatchError(
                    "Exception and traceback could not be found for K8S Job."
                )
            error_traceback = Traceback.from_error(error)
    else:
        # Script task.
        if error_file.exists():
            error = ScriptError(cast(bytes, error_file.read("rb")))
        else:
            error = K8SBatchError("stderr could not be found for K8S Job.")
        error_traceback = Traceback.from_error(error)

    return error, error_traceback


def parse_task_logs(
    k8s_job_id: str,
    max_lines: int = 1000,
    required: bool = True,
) -> Iterator[str]:
    """
    Iterates through most recent logs of an K8S Job.
    """
    lines_iter = iter_k8s_job_log_lines(
        k8s_job_id, reverse=True, required=required,
    )
    lines = reversed(list(islice(lines_iter, 0, max_lines)))

    if next(lines_iter, None) is not None:
        yield "\n*** Earlier logs are truncated ***\n"
    yield from lines


def k8s_describe_jobs(
    job_ids: List[str], chunk_size: int = 100, 
) -> Iterator[dict]:
    """
    Returns K8S Job descriptions from the AWS API.
    """
    print("====== k8s_describe_jobs")
    api_instance = k8s_utils.get_k8s_batch_client()
    job_ids_delimited = ','.join(job_ids)
    label_selector = f"controller-uid in ({job_ids_delimited})"
    api_response = api_instance.list_job_for_all_namespaces(
        label_selector=label_selector)
    return api_response.items


def iter_k8s_job_status(
    job_ids: List[str], pending_truncate: int = 10, 
) -> Iterator[dict]:
    """
    Yields K8S jobs statuses.

    If pending_truncate is used (> 0) then rely on K8S's behavior of running
    jobs approximately in order. This allows us to truncate the polling of jobs
    once we see a sufficient number of pending jobs.

    Parameters
    ----------
    job_ids : List[str]
      Batch job ids that should be in order of submission.
    pending_truncate : int
      After seeing `pending_truncate` number of pending jobs, assume the rest are pending.
      Use a negative int to disable this optimization.
    aws_region : str
       AWS region that jobs are running in.
    """
    print("====== iter_k8s_job_status")
    pending_run = 0

    for job in k8s_describe_jobs(job_ids):
        yield job

    #     if job["status"] in BATCH_JOB_STATUSES.pending:
    #         pending_run += 1
    #     else:
    #         pending_run = 0

    #     if pending_truncate > 0 and pending_run > pending_truncate:
    #         break


def iter_log_stream(
    job_id: str,
    limit: Optional[int] = None,
    reverse: bool = False,
    required: bool = True,
) -> Iterator[dict]:
    """
    Iterate through the events of a K8S log.
    """
    job = k8s_describe_jobs([job_id])[0]
    api_instance = k8s_utils.get_k8s_core_client()
    label_selector = f"job-name={job.metadata.name}"
    api_response = api_instance.list_pod_for_all_namespaces(
        label_selector=label_selector)
    name = api_response.items[0].metadata.name
    namespace = api_response.items[0].metadata.namespace
    log_response = api_instance.read_namespaced_pod_log(name, namespace=namespace)
    lines = log_response.split("\n")
    
    if reverse:
        lines = reversed(lines)
        yield from lines


# Unused
# TODO(davidek): figure out if we need to format the logs correct (withi timestamps?)
def format_log_stream_event(event: dict) -> str:
    """
    Format a logStream event as a line.
    """
    import pdb; pdb.set_trace()
    timestamp = str(datetime.datetime.fromtimestamp(event["timestamp"] / 1000))
    return "{timestamp}  {message}".format(timestamp=timestamp, message=event["message"])


def iter_k8s_job_logs(
    job_id: str,
    limit: Optional[int] = None,
    reverse: bool = False,
    required: bool = True,
) -> Iterator[dict]:
    """
    Iterate through the log events of an K8S job.
    """

    yield from iter_log_stream(
        job_id=job_id,
        limit=limit,
        reverse=reverse,
        required=required,
    )


def iter_k8s_job_log_lines(
    job_id: str,
    reverse: bool = False,
    required: bool = True,
) -> Iterator[str]:
    """
    Iterate through the log lines of an K8S job.
    """
    log_lines = iter_k8s_job_logs(
        job_id,
        reverse=reverse,
        required=required,
    )
    return log_lines


def iter_local_job_status(s3_scratch_prefix: str, job_id2job: Dict[str, "Job"]) -> Iterator[dict]:
    """
    Returns local Docker jobs grouped by their status.
    """
    running_containers = subprocess.check_output(["docker", "ps", "--no-trunc"]).decode("utf8")

    for job_id, redun_job in job_id2job.items():
        if job_id not in running_containers:
            # Job is done running.
            status_file = File(
                aws_utils.get_job_scratch_file(
                    s3_scratch_prefix, redun_job, s3_utils.S3_SCRATCH_STATUS
                )
            )
            output_file = File(
                aws_utils.get_job_scratch_file(
                    s3_scratch_prefix, redun_job, s3_utils.S3_SCRATCH_OUTPUT
                )
            )

            # Get docker logs and remove container.
            logs = subprocess.check_output(["docker", "logs", job_id]).decode("utf8")
            logs += "Removing container...\n"
            logs += subprocess.check_output(["docker", "rm", job_id]).decode("utf8")

            # TODO: Simplify whether status file is always used or not.
            if status_file.exists():
                succeeded = status_file.read().strip() == "ok"
            else:
                succeeded = output_file.exists()

            status = SUCCEEDED if succeeded else FAILED
            yield {"jobId": job_id, "status": status, "logs": logs}


class AWSBatchError(Exception):
    pass


class AWSBatchJobTimeoutError(Exception):
    """
    Custom exception to raise when K8S Jobs are killed due to timeout.
    """

    pass


@register_executor("k8s")
class K8SExecutor(Executor):
    def __init__(self, name: str, scheduler: Optional["Scheduler"] = None, config=None):
        super().__init__(name, scheduler=scheduler)
        if config is None:
            raise ValueError("K8SExecutor requires config.")

        # Required config.
        self.image = config["image"]
        self.queue = config["queue"]
        self.s3_scratch_prefix = config["s3_scratch"]

        # Optional config.
        self.role = config.get("role")
        self.code_package = aws_utils.parse_code_package_config(config)
        self.code_file: Optional[File] = None
        self.debug = config.getboolean("debug", fallback=False)

        # Default task options.
        self.default_task_options = {
            "vcpus": config.getint("vcpus", 1),
            "gpus": config.getint("gpus", 0),
            "memory": config.getint("memory", 4),
            "retries": config.getint("retries", 1),
            "role": config.get("role"),
            "job_name_prefix": config.get("job_name_prefix", "redun-job"),
        }
        if config.get("k8s_tags"):
            self.default_task_options["k8s_tags"] = json.loads(config.get("k8s_tags"))
        self.use_default_k8s_tags = config.getboolean("default_k8s_tags", True)

        self.is_running = False
        # We use an OrderedDict in order to retain submission order.
        self.pending_k8s_jobs: Dict[str, "Job"] = OrderedDict()
        self.preexisting_k8s_jobs: Dict[str, str] = {}  # Job hash -> Job ID

        if not self.debug:
            self.interval = config.getfloat("job_monitor_interval", 5.0)
        else:
            self.interval = config.getfloat("job_monitor_interval", 0.2)

        self.arrayer = JobArrayer(
            executor=self,
            submit_interval=self.interval,
            stale_time=config.getfloat("job_stale_time", 3.0),
            min_array_size=config.getint("min_array_size", 5),
            max_array_size=config.getint("max_array_size", 1000),
        )
        self._aws_user: Optional[str] = None

    def gather_inflight_jobs(self) -> None:
        print("====== gather_inflight_jobs")

        running_arrays: Dict[str, List[Tuple[str, int]]] = defaultdict(list)

        # Get all running jobs by name
        inflight_jobs = self.get_jobs([]) #BATCH_JOB_STATUSES.inflight)
        for job in inflight_jobs.items:
            job_name = job.metadata.name
            job_id = job.metadata.uid
            print("job_name: ", job_name)
            print("job_id: ", job_id)
            # Single jobs can be simply added to dict of pre-existing jobs.
            if not is_array_job_name(job_name):
                job_hash = get_hash_from_job_name(job_name)
                if job_hash:
                    self.preexisting_k8s_jobs[job_hash] = job.metadata.uid
                continue

        #     # Get all child jobs of running array jobs for reuniting.
        #     running_arrays[name] = [
        #         (child_job["jobId"], child_job["arrayProperties"]["index"])
        #         for child_job in self.get_array_child_jobs(
        #             job["jobId"], BATCH_JOB_STATUSES.inflight
        #         )
        #     ]
        # # Match up running array jobs with consistent redun job naming scheme.
        # for array_name, child_job_indices in running_arrays.items():

        #     # Get path to array file directory on S3 from array job name.
        #     parent_hash = get_hash_from_job_name(array_name)
        #     if not parent_hash:
        #         continue
        #     eval_file = File(
        #         aws_utils.get_array_scratch_file(
        #             self.s3_scratch_prefix, parent_hash, s3_utils.S3_SCRATCH_HASHES
        #         )
        #     )
        #     if not eval_file.exists():
        #         # Eval file does not exist, so we cannot reunite with this array job.
        #         continue

        #     # Get eval_hash for all jobs that were part of the array
        #     eval_hashes = cast(str, eval_file.read("r")).splitlines()

        #     # Now match up indices to eval hashes to populate pending jobs by name.
        #     for job_id, job_index in child_job_indices:
        #         job_hash = eval_hashes[job_index]
        #         self.preexisting_k8s_jobs[job_hash] = job_id

    def _start(self) -> None:
        """
        Start monitoring thread.
        """
        print("====== _start")
        if not self.is_running:
            # self._aws_user = aws_utils.get_aws_user()

            self.is_running = True
            self._thread = threading.Thread(target=self._monitor, daemon=False)
            self._thread.start()

    def stop(self) -> None:
        """
        Stop Executor and monitoring thread.
        """
        self.arrayer.stop()
        self.is_running = False

    def _monitor(self) -> None:
        """
        Thread for monitoring running K8S jobs.

        We use the following process for monitoring K8S jobs in order to
        achieve timely updates and avoid excessive API calls which can cause
        API throttling and slow downs.

        - We use the `describe_jobs()` API call on specific Batch job ids in order
          to avoid processing status of unrelated jobs on the same K8S queue.
        - We call `describe_jobs()` with 100 job ids at a time to reduce the number
          of API calls. 100 job ids is the maximum supported amount by
          `describe_jobs()`.
        - We do only one describe_jobs() API call per monitor loop, and then
          sleep `self.interval` seconds.
        - K8S runs jobs in approximately the order submitted. So if we
          monitor job statuses in submission order, a run of PENDING statuses
          (`pending_truncate`) suggests the rest of the jobs will be PENDING.
          Therefore, we can truncate our polling and restart at the beginning
          of list of job ids.

        By combining these techniques, we spend most of our time monitoring
        only running jobs (there could be a very large number of pending jobs),
        we stay under API rate limits, and we keep the compute in this
        thread low so as to not interfere with new submissions.
        """
        print("====== _start")
        assert self.scheduler
        chunk_size = 100
        pending_truncate = 10

        try:
            print("============ _monitor")
            print("is_running:", self.is_running)
            print(self.pending_k8s_jobs)
            while self.is_running and (self.pending_k8s_jobs or self.arrayer.num_pending):

                print("====== monitort loop")
                if self.scheduler.logger.level >= logging.DEBUG:
                    self.log(
                        f"Preparing {self.arrayer.num_pending} job(s) for Job Arrays.",
                        level=logging.DEBUG,
                    )
                    self.log(
                        f"Waiting on {len(self.pending_k8s_jobs)} K8S job(s): "
                        + " ".join(sorted(self.pending_k8s_jobs.keys())),
                        level=logging.DEBUG,
                    )
                if not self.debug:
                    # Copy pending_k8s_jobs.keys() since it can change due to new submissions.
                    print("Checking for", list(self.pending_k8s_jobs.keys()), self.arrayer.num_pending)
                    jobs = k8s_describe_jobs(list(self.pending_k8s_jobs.keys()))
                    for job in jobs:
                        self._process_job_status(job)
                        
                else:
                    # Copy pending_k8s_jobs since it can change due to new submissions.
                    jobs = iter_local_job_status(
                        self.s3_scratch_prefix, dict(self.pending_k8s_jobs)
                    )
                    for job in jobs:
                        self._process_job_status(job)
                time.sleep(self.interval)

        except Exception as error:
            # Since we run this is method at the top-level of a thread, we
            # need to catch all exceptions so we can properly report them to
            # the scheduler.
            self.scheduler.reject_job(None, error)

        self.log("Shutting down executor...", level=logging.DEBUG)
        self.stop()

    def _can_override_failed(self, job: dict) -> Tuple[bool, str]:
        """
        Certain AWS errors can be ignored that do not effect the result.

        https://github.com/aws/amazon-ecs-agent/issues/2312
        """
        container_reason = "k8s stub"

        # try:
        #     container_reason = job["attempts"][-1]["container"]["reason"]
        # except (KeyError, IndexError):
        #     container_reason = ""

        # if DOCKER_INSPECT_ERROR in container_reason:
        #     redun_job = self.pending_k8s_jobs[job["jobId"]]
        #     assert redun_job.task
        #     if redun_job.task.script:
        #         # Script tasks will report their status in a status file.
        #         status_file = File(
        #             aws_utils.get_job_scratch_file(
        #                 self.s3_scratch_prefix, redun_job, s3_utils.S3_SCRATCH_STATUS
        #             )
        #         )
        #         if status_file.exists():
        #             return status_file.read().strip() == "ok", container_reason
        #     else:
        #         # Non-script tasks only create an output file if it is successful.
        #         output_file = File(
        #             aws_utils.get_job_scratch_file(
        #                 self.s3_scratch_prefix, redun_job, s3_utils.S3_SCRATCH_OUTPUT
        #             )
        #         )
        #         return output_file.exists(), container_reason

        return False, container_reason

    def _process_job_status(self, job: dict) -> None:
        """
        Process K8S job statuses.
        """
        print("======== _process_job_stats")
        assert self.scheduler
        job_status: Optional[str] = None
        # Determine job status.
        if job.status.succeeded is not None and job.status.succeeded > 0:
            job_status = SUCCEEDED
        elif job.status.failed is not None and job.status.failed > 0:
            can_override, container_reason = self._can_override_failed(job)
            if can_override:
                job_status = SUCCEEDED
                self.scheduler.log("NOTE: Overriding K8S error: {}".format(container_reason))
            else:
                job_status = FAILED
        else:
            print("Skipping job with status:", job.status)
            return

        # Determine redun Job and job_tags.
        redun_job = self.pending_k8s_jobs.pop(job.metadata.uid)
        job_tags = []
        if not self.debug:
            job_tags.append(("k8s_job", job.metadata.uid))
            # log_stream = job.get("container", {}).get("logStreamName")
            # if log_stream:
            #     job_tags.append(("aws_log_stream", log_stream))

        if job_status == SUCCEEDED:
            # Assume a recently completed job has valid results.
            result, exists = self._get_job_output(redun_job, check_valid=False)
            if exists:
                self.scheduler.done_job(redun_job, result, job_tags=job_tags)
            else:
                # This can happen if job ended in an inconsistent state.
                self.scheduler.reject_job(
                    redun_job,
                    FileNotFoundError(
                        aws_utils.get_job_scratch_file(
                            self.s3_scratch_prefix, redun_job, s3_utils.S3_SCRATCH_OUTPUT
                        )
                    ),
                    job_tags=job_tags,
                )
        elif job_status == FAILED:
            error, error_traceback = parse_task_error(
                self.s3_scratch_prefix, redun_job, k8s_job_metadata=job
            )
            if not self.debug:
                logs = [f"*** CloudWatch logs for K8S job {job.metadata.uid}:\n"]
                if container_reason:
                    logs.append(f"container.reason: {container_reason}\n")

                try:
                    status_reason = job.status.conditions[-1].message
                except (KeyError, IndexError):
                    status_reason = ""
                if status_reason:
                    logs.append(f"statusReason: {status_reason}\n")

                logs.extend(
                    parse_task_logs(job.metadata.uid, required=False)
                )
                error_traceback.logs = logs
            else:
                error_traceback.logs = [line + "\n" for line in job["logs"].split("\n")]
            self.scheduler.reject_job(
                redun_job, error, error_traceback=error_traceback, job_tags=job_tags
            )

    def _get_job_options(self, job: Job) -> dict:
        """
        Determine the task options for a job.

        Task options can be specified at the job-level have precedence over
        the executor-level (within `redun.ini`):
        """
        assert job.task

        job_options = job.get_options()

        task_options = {
            **self.default_task_options,
            **job_options,
        }

        # Add default k8s tags to the job.
        if self.use_default_k8s_tags:
            execution = job.execution
            project = (
                execution.job.task.namespace
                if execution and execution.job and execution.job.task
                else ""
            )
            default_tags = {
                "redun_job_id": job.id,
                "redun_task_name": job.task.fullname,
                "redun_execution_id": execution.id if execution else "",
                "redun_project": project,
                "redun_aws_user": self._aws_user or "",
            }
        else:
            default_tags = {}

        # Merge k8s_tags if needed.
        k8s_tags = {
            **self.default_task_options.get("k8s_tags", {}),
            **default_tags,
            **job_options.get("k8s_tags", {}),
        }
        if k8s_tags:
            task_options["k8s_tags"] = k8s_tags

        return task_options

    def _get_job_output(self, job: Job, check_valid: bool = True) -> Tuple[Any, bool]:
        """
        Return job output if it exists.

        Returns a tuple of (result, exists).
        """
        assert self.scheduler

        output_file = File(
            aws_utils.get_job_scratch_file(
                self.s3_scratch_prefix, job, s3_utils.S3_SCRATCH_OUTPUT
            )
        )
        if output_file.exists():
            result = aws_utils.parse_task_result(self.s3_scratch_prefix, job)
            if not check_valid or self.scheduler.is_valid_value(result):
                return result, True
        return None, False

    def _submit(self, job: Job, args: Tuple, kwargs: dict) -> None:
        """
        Submit Job to executor.
        """
        print("======== _submit")
        assert self.scheduler
        assert job.task

        # If we are not in debug mode and this is the first submission gather inflight jobs. In
        # debug mode, we are running on docker locally so there is no need to hit the K8S API to
        # gather jobs as we are not going to run on K8S. We also check is_running here as a way
        # of determining whether this is the first submission or not. If we are already running,
        # then we know we have already had jobs submitted and done the inflight check so no
        # reason to do that again here.
        if not self.debug and not self.is_running:
            # Precompute existing inflight jobs for job reuniting.
            self.gather_inflight_jobs()

        # Package code if necessary and we have not already done so. If code_package is False,
        # then we can skip this step. Additionally, if we have already packaged and set code_file,
        # then we do not need to repackage.
        if self.code_package is not False and self.code_file is None:
            code_package = self.code_package or {}
            assert isinstance(code_package, dict)
            self.code_file = aws_utils.package_code(self.s3_scratch_prefix, code_package)

        job_dir = aws_utils.get_job_scratch_dir(self.s3_scratch_prefix, job)
        job_type = "K8S job" if not self.debug else "Docker container"

        # Determine job options.
        task_options = self._get_job_options(job)
        use_cache = task_options.get("cache", True)

        # # Determine if we can reunite with a previous K8S output or job.
        k8s_job_id: Optional[str] = None
        if use_cache and job.eval_hash in self.preexisting_k8s_jobs:
            k8s_job_id = self.preexisting_k8s_jobs.pop(job.eval_hash)
            print("got k8s_job_id", k8s_job_id)

            # Make sure k8s API still has a status on this job.
            existing_job = k8s_describe_jobs([k8s_job_id])[0]

            # Reunite with inflight k8s job, if present.
            if existing_job:
                k8s_job_id = existing_job.metadata.uid
                self.log(
                    "reunite redun job {redun_job} with {job_type} {k8s_job}:\n"
                    "  s3_scratch_path = {job_dir}".format(
                        redun_job=job.id,
                        job_type=job_type,
                        k8s_job=k8s_job_id,
                        job_dir=job_dir,
                    )
                )
                assert k8s_job_id
                self.pending_k8s_jobs[k8s_job_id] = job
            else:
                k8s_job_id = None

        # Job arrayer will handle actual submission after bunching to an array
        # job, if necessary.
        if k8s_job_id is None:
            self.arrayer.add_job(job, args, kwargs)

        self._start()

    def _submit_array_job(
        self, jobs: List[Job], all_args: List[Tuple], all_kwargs: List[Dict]
    ) -> str:
        """Submits an array job, returning job name uuid"""
        print("====== _submit_array_job")
        array_size = len(jobs)
        assert array_size == len(all_args) == len(all_kwargs)

        # All jobs identical so just grab the first one
        job = jobs[0]
        assert job.task
        if job.task.script:
            raise NotImplementedError("Array jobs not supported for scripts")

        task_options = self._get_job_options(job)
        image = task_options.pop("image", self.image)
        queue = task_options.pop("queue", self.queue)
        # Generate a unique name for job with no '-' to simplify job name parsing.
        array_uuid = str(uuid.uuid4()).replace("-", "")

        job_type = "K8S job" if not self.debug else "Docker container"

        # Setup input, output and error path files.
        # Input file is a pickled list of args, and kwargs, for each child job.
        input_file = aws_utils.get_array_scratch_file(
            self.s3_scratch_prefix, array_uuid, s3_utils.S3_SCRATCH_INPUT
        )
        with File(input_file).open("wb") as out:
            pickle_dump([all_args, all_kwargs], out)

        # Output file is a plaintext list of output paths, for each child job.
        output_file = aws_utils.get_array_scratch_file(
            self.s3_scratch_prefix, array_uuid, s3_utils.S3_SCRATCH_OUTPUT
        )
        output_paths = [
            aws_utils.get_job_scratch_file(
                self.s3_scratch_prefix, job, s3_utils.S3_SCRATCH_OUTPUT
            )
            for job in jobs
        ]
        with File(output_file).open("w") as ofile:
            json.dump(output_paths, ofile)

        # Error file is a plaintext list of error paths, one for each child job.
        error_file = aws_utils.get_array_scratch_file(
            self.s3_scratch_prefix, array_uuid, s3_utils.S3_SCRATCH_ERROR
        )
        error_paths = [
            aws_utils.get_job_scratch_file(self.s3_scratch_prefix, job, s3_utils.S3_SCRATCH_ERROR)
            for job in jobs
        ]
        with File(error_file).open("w") as efile:
            json.dump(error_paths, efile)

        # Eval hash file is plaintext hashes of child jobs for matching for job reuniting.
        eval_file = aws_utils.get_array_scratch_file(
            self.s3_scratch_prefix, array_uuid, s3_utils.S3_SCRATCH_HASHES
        )
        with File(eval_file).open("w") as eval_f:
            eval_f.write("\n".join([job.eval_hash for job in jobs]))  # type: ignore

        k8s_resp = submit_task(
            image,
            queue,
            self.s3_scratch_prefix,
            job,
            job.task,
            job_options=task_options,
            debug=self.debug,
            code_file=self.code_file,
            aws_region=self.aws_region,
            array_uuid=array_uuid,
            array_size=array_size,
        )

        if self.debug:
            # Debug mode just starts N docker containers
            array_job_id = "None"
            for i in range(array_size):
                self.pending_k8s_jobs[k8s_resp["jobId"][i]] = jobs[i]
        else:
            # Add entire array to array jobs, and all jobs in array to pending jobs.
            array_job_id = k8s_resp["jobId"]
            for i in range(array_size):
                self.pending_k8s_jobs[f"{array_job_id}:{i}"] = jobs[i]

        self.log(
            "submit {array_size} redun job(s) as {job_type} {k8s_job}:\n"
            "  array_job_id    = {array_job_id}\n"
            "  array_job_name  = {job_name}\n"
            "  array_size      = {array_size}\n"
            "  s3_scratch_path = {job_dir}\n"
            "  retry_attempts  = {retries}\n"
            "  debug           = {debug}".format(
                array_job_id=array_job_id,
                array_size=array_size,
                job_type=job_type,
                k8s_job=array_job_id,
                job_dir=aws_utils.get_array_scratch_file(self.s3_scratch_prefix, array_uuid, ""),
                job_name=k8s_resp.get("jobName"),
                retries=k8s_resp.get("ResponseMetadata", {}).get("RetryAttempts"),
                debug=self.debug,
            )
        )

        return array_uuid

    def _submit_single_job(self, job: Job, args: Tuple, kwargs: dict) -> None:
        """
        Actually submits a job. Caching detects if it should be part
        of an array job
        """
        print("====== _submit_single_job")
        assert job.task
        task_options = self._get_job_options(job)
        image = task_options.pop("image", self.image)


        queue = None
        job_dir = aws_utils.get_job_scratch_dir(self.s3_scratch_prefix, job)
        #job_type = "K8S job" if not self.debug else "Docker container"

        # Submit a new Batch job.
        if not job.task.script:
            k8s_resp = submit_task(
                image,
                queue,
                self.s3_scratch_prefix,
                job,
                job.task,
                args=args,
                kwargs=kwargs,
                job_options=task_options,
                debug=self.debug,
                code_file=self.code_file,
            )
        else:
            command = get_task_command(job.task, args, kwargs)
            k8s_resp = submit_command(
                image,
                queue,
                self.s3_scratch_prefix,
                job,
                command,
                job_options=task_options,
                debug=self.debug,
            )

        job_type = "k8s"
        job_id = k8s_resp.metadata.uid
        job_name = k8s_resp.metadata.name
        retries = None # k8s_resp.get("ResponseMetadata", {}).get("RetryAttempts")
        self.log(
            "submit redun job {redun_job} as {job_type} {job_id}:\n"
            "  job_id          = {job_id}\n"
            "  job_name        = {job_name}\n"
            "  s3_scratch_path = {job_dir}\n"
            "  retry_attempts  = {retries}\n"
            "  debug           = {debug}".format(
                debug=self.debug,
                redun_job=job.id,
                job_type=job_type,
                job_id=job_id, 
                job_dir=job_dir,
                job_name=job_name,
                retries=retries,
            )
        )
        self.pending_k8s_jobs[job_id] = job

    def submit(self, job: Job, args: Tuple, kwargs: dict) -> None:
        """
        Submit Job to executor.
        """
        print("====== submit")
        return self._submit(job, args, kwargs)

    def submit_script(self, job: Job, args: Tuple, kwargs: dict) -> None:
        """
        Submit Job for script task to executor.
        """
        print("====== submit script")
        return self._submit(job, args, kwargs)

    def get_jobs(self, statuses: Optional[List[str]] = None) -> Iterator[dict]:
        """
        Returns K8S Job statuses from the AWS API.
        """
        print("==== get jobs")
        api_instance = k8s_utils.get_k8s_batch_client()
        api_response = api_instance.list_job_for_all_namespaces(watch=False)
        return api_response
        

    def get_array_child_jobs(
        self, job_id: str, statuses: List[str] = [] #BATCH_JOB_STATUSES.inflight
    ) -> List[Dict[str, Any]]:
        print("====== get_array_)child_jobs")
        #batch_client = aws_utils.get_aws_client("batch", aws_region=self.aws_region)
        #paginator = batch_client.get_paginator("list_jobs")

        #found_jobs = []
        #for status in statuses:
        #    pages = paginator.paginate(arrayJobId=job_id, jobStatus=status)
        #    found_jobs.extend([job for response in pages for job in response["jobSummaryList"]])

        api_instance = k8s_utils.get_k8s_batch_client()

        # job_name = 'redunjob'
        # api_response = api_instance.read_namespaced_job_status(
        #     name=job_name,
        #     namespace="default")
        return api_response

    def kill_jobs(
        self, job_ids: Iterable[str], reason: str = "Terminated by user"
    ) -> Iterator[dict]:
        """
        Kill K8S Jobs.
        """
        batch_client = aws_utils.get_aws_client("batch", aws_region=self.aws_region)

        for job_id in job_ids:
            yield batch_client.terminate_job(jobId=job_id, reason=reason)
