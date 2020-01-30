"""
To push projects asynchronously. Start push: (does not block)

job = push_project_async(mergin_client, '/tmp/my_project')

Then we need to wait until we are finished uploading - either by periodically
calling push_project_is_running(job) that will just return True/False or by calling
push_project_wait(job) that will block the current thread (not good for GUI).
To finish the upload job, we have to call push_project_finalize(job).
"""

import json
import hashlib
import concurrent.futures
import threading

from .client import UPLOAD_CHUNK_SIZE, ClientError, MerginProject


class UploadJob:
    """ Keeps all the important data about a pending upload job """
    
    def __init__(self, project_path, changes, transaction_id, mp, mc):
        self.project_path = project_path       # full project name ("username/projectname")
        self.changes = changes                 # dictionary of local changes to the project
        self.transaction_id = transaction_id   # ID of the transaction assigned by the server
        self.total_size = 0                    # size of data to upload (in bytes)
        self.transferred_size = 0              # size of data already uploaded (in bytes)
        self.upload_queue_items = []           # list of items to upload in the background
        self.mp = mp                           # MerginProject instance
        self.mc = mc                           # MerginClient instance
        self.is_cancelled = False              # whether upload has been cancelled
        self.executor = None                   # ThreadPoolExecutor that manages background upload tasks
        self.futures = []                      # list of futures submitted to the executor
        self.server_resp = None                # server response when transaction is finished

    def dump(self):
        print("--- JOB ---", self.total_size, "bytes")
        for item in self.upload_queue_items:
            print("- {} {} {}".format(item.file_path, item.chunk_index, item.size))
        print("--- END ---")


class UploadQueueItem:
    """ A single chunk of data that needs to be uploaded """
    
    def __init__(self, file_path, size, transaction_id, chunk_id, chunk_index):
        self.file_path = file_path            # full path to the file
        self.size = size                      # size of the chunk in bytes
        self.chunk_id = chunk_id              # ID of the chunk within transaction
        self.chunk_index = chunk_index        # index (starting from zero) of the chunk within the file
        self.transaction_id = transaction_id  # ID of the transaction
    
    def upload_blocking(self, mc):
        
        file_handle = open(self.file_path, 'rb')
        file_handle.seek(self.chunk_index * UPLOAD_CHUNK_SIZE)
        data = file_handle.read(UPLOAD_CHUNK_SIZE)
        
        checksum = hashlib.sha1()
        checksum.update(data)
        
        headers = {"Content-Type": "application/octet-stream"}
        resp = mc.post("/v1/project/push/chunk/{}/{}".format(self.transaction_id, self.chunk_id), data, headers)
        resp_dict = json.load(resp)
        if not (resp_dict['size'] == len(data) and resp_dict['checksum'] == checksum.hexdigest()):
            mc.post("/v1/project/push/cancel/{}".format(self.transaction_id))
            raise ClientError("Mismatch between uploaded file chunk {} and local one".format(self.chunk_id))
        


def push_project_async(mc, directory):
    """ Starts push of a project and returns pending upload job """

    mp = MerginProject(directory)
    project_path = mp.metadata["name"]
    local_version = mp.metadata["version"]
    server_info = mc.project_info(project_path)
    server_version = server_info["version"] if server_info["version"] else "v0"
    if local_version != server_version:
        raise ClientError("Update your local repository")

    changes = mp.get_push_changes()
    enough_free_space, freespace = mc.enough_storage_available(changes)
    if not enough_free_space:
        freespace = int(freespace/(1024*1024))
        raise SyncError("Storage limit has been reached. Only " + str(freespace) + "MB left")

    if not sum(len(v) for v in changes.values()):
        return
    # drop internal info from being sent to server
    for item in changes['updated']:
        item.pop('origin_checksum', None)
    data = {
        "version": local_version,
        "changes": changes
    }

    resp = mc.post(f'/v1/project/push/{project_path}', data, {"Content-Type": "application/json"})
    server_resp = json.load(resp)

    upload_files = data['changes']["added"] + data['changes']["updated"]

    transaction_id = server_resp["transaction"] if upload_files else None
    job = UploadJob(project_path, changes, transaction_id, mp, mc)

    if not upload_files:
        job.server_resp = server_resp
        push_project_finalize(job)
        return None   # all done - no pending job
    
    upload_queue_items = []
    total_size = 0
    # prepare file chunks for upload
    for file in upload_files:
        # do checkpoint to push changes from wal file to gpkg if there is no diff
        if "diff" not in file and mp.is_versioned_file(file["path"]):
            do_sqlite_checkpoint(mp.fpath(file["path"]))
            file["checksum"] = generate_checksum(mp.fpath(file["path"]))
        file['location'] = mp.fpath_meta(file['diff']['path']) if 'diff' in file else mp.fpath(file['path'])

        for chunk_index, chunk_id in enumerate(file["chunks"]):
            size = min(UPLOAD_CHUNK_SIZE, file['size'] - chunk_index * UPLOAD_CHUNK_SIZE)
            upload_queue_items.append(UploadQueueItem(file['location'], size, transaction_id, chunk_id, chunk_index))

        total_size += file['size']

    job.total_size = total_size
    job.upload_queue_items = upload_queue_items
    
    # start uploads in background
    job.executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)
    for item in upload_queue_items:
        future = job.executor.submit(_do_upload, item, job)
        job.futures.append(future)

    return job



def push_project_wait(job):
    """ blocks until all upload tasks are finished """
    
    concurrent.futures.wait(job.futures)

    # handling of exceptions
    for future in job.futures:
        if future.exception() is not None:
            raise future.exception()


def push_project_is_running(job):
    """ Returns true/false depending on whether we have some pending uploads """
    for future in job.futures:
        if future.running():
            return True
    return False


def push_project_finalize(job):

    with_upload_of_files = job.executor is not None

    if with_upload_of_files:
        job.executor.shutdown(wait=True)

    assert job.transferred_size == job.total_size

    if with_upload_of_files:
        try:
            resp = job.mc.post("/v1/project/push/finish/%s" % job.transaction_id)
            job.server_resp = json.load(resp)
        except ClientError as err:
            job.mc.post("/v1/project/push/cancel/%s" % job.transaction_id)
            # server returns various error messages with filename or something generic
            # it would be better if it returned list of failed files (and reasons) whenever possible
            return {'error': str(err)}
    
    if 'error' in job.server_resp:
        #TODO would be good to get some detailed info from server so user could decide what to do with it
        # e.g. diff conflicts, basefiles issues, or any other failure
        raise ClientError(job.server_resp['error'])

    job.mp.metadata = {
        'name': job.project_path,
        'version': job.server_resp['version'],
        'files': job.server_resp["files"]
    }
    job.mp.apply_push_changes(job.changes)


def push_project_cancel(job):
    """
    To be called (from main thread) to cancel a job that has uploads in progress.
    Returns once all background tasks have exited (may block for a bit of time).
    """
    
    # set job as cancelled
    job.is_cancelled = True

    job.executor.shutdown(wait=True)


def _do_upload(item, job):
    """ runs in worker thread """
    #print(threading.current_thread(), "uploading", item.file_path)
    if job.is_cancelled:
        #print(threading.current_thread(), "uploading", item.file_path, "cancelled")
        return
    
    item.upload_blocking(job.mc)
    job.transferred_size += item.size
    #print(threading.current_thread(), "uploading", item.file_path, "finished")


if __name__ == '__main__':
    
    from .client import MerginClient, MerginProject
    import shutil
    
    decoded = lambda x: "".join(chr(ord(c) ^ 13) for c in x)
    
    auth_token = 'Bearer XXXX_replace_XXXX'
    mc = MerginClient("https://public.cloudmergin.com/", auth_token)
    
    # create new project
    test_dir = "/tmp/_mergin_fibre"
    project_name = "test_upload_async"
    print("deleting")
    mc.delete_project("martin/"+project_name)
    print("creating")
    mc.create_project(project_name)
    print("pushing")

    # initialize so that mergin client can use it
    mp = MerginProject(test_dir)
    mp.metadata = { "name": "martin/"+project_name, "version": "v0", "files": [] }
    
    job = push_project_async(mc, test_dir)
    job.dump()
    push_project_wait(job)
    push_project_finalize(job)
