from __future__ import with_statement
from __future__ import absolute_import
from io import BytesIO
import json
import mimetypes
import os
import re
import shutil
import tarfile

from django import forms
from django.contrib.staticfiles.templatetags.staticfiles import static
from django.core.files import File
from django.core.files.storage import default_storage
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.utils._os import safe_join
from django.utils.six.moves.urllib.parse import urljoin
from django.views.generic import View

from PIL import Image

from . import settings
from io import open

KB = 1024

ActionChoices = (
    (u'upload', u'upload'),
    (u'rename', u'rename'),
    (u'delete', u'delete'),
    (u'add', u'add'),
    (u'move', u'move'),
    (u'copy', u'copy'),
)


def is_valid_filename(name):
    return not re.match(ur'[^-\w\d \./]', name)


def is_valid_dirname(name):
    return is_valid_filename(name)


class FileManagerForm(forms.Form):
    ufile = forms.FileField(required=False)
    action = forms.ChoiceField(choices=ActionChoices)
    path = forms.CharField(max_length=200, required=False)
    name = forms.CharField(max_length=32, required=False)
    current_path = forms.CharField(max_length=200, required=False)
    file_or_dir = forms.CharField(max_length=4)

    def clean_path(self):
        return self.cleaned_data[u'path'].lstrip(u'/')


class FileManager(View):
    u"""
    maxspace,maxfilesize in KB
    """
    basepath = None
    maxfolders = 50
    maxspace = 5 * KB
    maxfilesize = 1 * KB
    extensions = []
    public_url_base = None

    def dispatch(self, *args, **kwargs):
        self.idee = 0
        import pdb; pdb.set_trace()
        if u'download' in self.request.GET:
            return self.download(None, self.request.GET[u'download'])
        if kwargs.get('path'):
            return self.media(kwargs['path'])
        messages = []
        self.current_path = u'/'
        self.current_id = 1
        if self.request.method == u'POST':
            form = FileManagerForm(self.request.POST, self.request.FILES)
            if form.is_valid():
                messages = self.handle_form(form, self.request.FILES)
        if settings.FILEMANAGER_CHECK_SPACE:
            space_consumed = self.get_size(self.basepath)
        else:
            space_consumed = 0
        return render(self.request, u'filemanager/index.html', {
            u'dir_structure': json.dumps(self.directory_structure()),
            u'messages': [unicode(m) for m in messages],
            u'current_id': self.current_id,
            u'public_url_base': self.public_url_base,
            u'space_consumed': space_consumed,
            u'max_space': self.maxspace,
            u'show_space': settings.FILEMANAGER_SHOW_SPACE,
        })

    # XXX Replace with with using storage API
    def rename_if_exists(self, folder, filename):
        if os.path.exists(safe_join(folder, filename)):
            root, ext = os.path.splitext(filename)
            if not ext:
                fmt = u'{root}.{i}'
            else:
                fmt = u'{root}.{i}.{ext}'
            for i in xrange(1000):
                filename = fmt.format(root=root, i=i, ext=ext)
                if not os.path.exists(safe_join(folder, filename)):
                    break
        return filename

    def get_size(self, start_path):
        total_size = 0
        for dirpath, dirnames, filenames, dir_fd in os.fwalk(start_path):
            total_size += sum(os.stat(f, dir_fd=dir_fd).st_size for f in filenames)
        return total_size

    def next_id(self):
        self.idee = self.idee + 1
        return self.idee

    def handle_form(self, form, files):
        action = form.cleaned_data[u'action']
        file_or_dir = form.cleaned_data[u'file_or_dir']
        self.current_path = form.cleaned_data[u'current_path']

        try:
            handler = getattr(self, u'do_{}_{}'.format(file_or_dir, action))
        except AttributeError:
            return [u'Action not supported!']
        else:
            return handler(files=files, **form.cleaned_data)

    def do_file_upload(self, **kwargs):
        if 'files' in kwargs: files = kwargs['files']; del kwargs['files']
        else: files = None
        if 'path' in kwargs: path = kwargs['path']; del kwargs['path']
        else: path = None
        messages = []
        for f in files.getlist(u'ufile'):
            root, ext = os.path.splitext(f.name)
            if not is_valid_filename(f.name):
                messages.append(u"File name is not valid : " + f.name)
            elif f.size > self.maxfilesize * KB:
                messages.append(u"File size exceeded {} KB : {}".format(self.maxfilesize, f.name))
            elif settings.FILEMANAGER_CHECK_SPACE and (self.get_size(self.basepath) + f.size) > self.maxspace * KB:
                messages.append(u"Total Space size exceeded {} KB: {}".format(self.maxspace, f.name))
            elif ext and self.extensions and ext.lower().lstrip(u'.') not in self.extensions:
                messages.append(u"File extension not allowed ({}) : {}".format(ext, f.name))
            elif not ext and self.extensions and root not in self.extensions:
                messages.append(u"No file extension in uploaded file : " + f.name)
            else:
                full_path = safe_join(self.basepath, path)
                filepath = safe_join(full_path, self.rename_if_exists(full_path, f.name))
                with open(filepath, u'wb') as dest:
                    for chunk in f.chunks():
                        dest.write(chunk)
                f.close()
        if not messages:
            messages.append(u'All files uploaded successfully')
        return messages

    def do_dir_rename(self, **kwargs):
        if 'name' in kwargs: name = kwargs['name']; del kwargs['name']
        else: name = None
        if 'path' in kwargs: path = kwargs['path']; del kwargs['path']
        else: path = None
        path, oldname = os.path.split(path)
        try:
            os.chdir(safe_join(self.basepath, path))
            os.rename(oldname, name)
        except:
            return [u"Folder couldn't renamed to {}".format(name)]
        return [u'Folder renamed successfully from {} to {}'.format(oldname, name)]

    def do_file_rename(self, **kwargs):
        if 'name' in kwargs: name = kwargs['name']; del kwargs['name']
        else: name = None
        if 'path' in kwargs: path = kwargs['path']; del kwargs['path']
        else: path = None
        path, oldname = os.path.split(path)
        _, old_ext = os.path.splitext(oldname)
        _, new_ext = os.path.splitext(name)
        if old_ext == new_ext:
            try:
                os.chdir(safe_join(self.basepath, path))
                os.rename(oldname, name)
            except:
                return [u"File couldn't be renamed to {}".format(name)]
            return [u'File renamed successfully from {} to {}'.format(oldname, name)]
        else:
            if old_ext:
                return [u'File extension should be same : .{}'.format(old_ext)]
            else:
                return [u"New file extension didn't match with old file extension"]

    def do_dir_delete(self, **kwargs):
        if 'path' in kwargs: path = kwargs['path']; del kwargs['path']
        else: path = None
        if path == u'':
            return [u"root folder can't be deleted"]
        else:
            full_path = safe_join(self.basepath, path)
            base_path, name = os.path.split(full_path)
            try:
                os.chdir(base_path)
                shutil.rmtree(name)
            except:
                return [u"Folder couldn't deleted : {}".format(name)]
            return [u'Folder deleted successfully : {}'.format(name)]

    def do_file_delete(self, **kwargs):
        if 'path' in kwargs: path = kwargs['path']; del kwargs['path']
        else: path = None
        if path == u'':
            return [u"root folder can't be deleted"]
        path, name = os.path.split(path)
        try:
            os.chdir(safe_join(self.basepath, path))
            os.remove(name)
        except:
            return [u"File couldn't deleted : {}".format(name)]
        return [u'File deleted successfully : {}'.format(name)]

    def do_dir_add(self, **kwargs):
        if 'name' in kwargs: name = kwargs['name']; del kwargs['name']
        else: name = None
        if 'path' in kwargs: path = kwargs['path']; del kwargs['path']
        else: path = None
        os.chdir(self.basepath)
        no_of_folders = len(list(os.walk(u'.')))
        if no_of_folders >= self.maxfolders:
            return [u"Folder couldn' be created because maximum number of folders exceeded : {}".format(self.maxfolders)]
        try:
            os.chdir(safe_join(self.basepath, path))
            os.mkdir(name)
        except:
            return [u"Folder couldn't be created : {}".format(name)]
        return [u'Folder created successfully : {}'.format(name)]

    def do_file_move(self, **kwargs):
        return self._more_or_copy(method=shutil.move, **kwargs)

    def do_dir_move(self, **kwargs):
        return self._more_or_copy(method=shutil.move, **kwargs)

    def do_file_copy(self, **kwargs):
        return self._move_or_copy(method=shutil.copy, **kwargs)

    def do_dir_copy(self, **kwargs):
        return self._move_or_copy(method=shutil.copytree, **kwargs)

    def _move_or_copy(self, **kwargs):
        # from path to current_path
        if 'path' in kwargs: path = kwargs['path']; del kwargs['path']
        else: path = None
        if 'method' in kwargs: method = kwargs['method']; del kwargs['method']
        else: method = None
        if self.current_path.find(path) == 0:
            return [u'Cannot move/copy to a child folder']
        path = os.path.normpath(path)  # strip trailing slash if any
        if os.path.exists(safe_join(self.basepath, self.current_path, os.path.basename(path))):
            return [u'ERROR: A file/folder with this name already exists in the destination folder.']
        try:
            method(safe_join(self.basepath, path),
                   safe_join(self.basepath, self.current_path, os.path.basename(path)))
        except:
            return [u"File/folder couldn't be moved/copied."]

        return []

    def directory_structure(self):
        self.idee = 0
        dir_structure = {
            u'': {
                u'id': self.next_id(),
                u'open': True,
                u'dirs': {},
                u'files': [],
            },
        }
        os.chdir(self.basepath)
        for directory, directories, files in os.walk(u'.'):
            directory_list = directory[1:].split(u'/')
            current_dir = None
            nextdirs = dir_structure
            for d in directory_list:
                current_dir = nextdirs[d]
                nextdirs = current_dir[u'dirs']
            if directory[1:] + u'/' == self.current_path:
                self.current_id = current_dir[u'id']
            current_dir[u'dirs'].update(dict((
                d, {
                    u'id': self.next_id(),
                    u'open': False,
                    u'dirs': {},
                    u'files': [],
                })
                for d in directories))
            current_dir[u'files'] = files
        return dir_structure

    def media(self, path):
        filename = os.path.basename(path)
        root, ext = os.path.splitext(filename)
        mimetype, _ = mimetypes.guess_type(filename)
        if mimetype and mimetype.startswith(u'image/'):
            if not path.startswith(settings.THUMBNAIL_PREFIX):
                # Generate target filename
                target_name = os.path.join(settings.THUMBNAIL_PREFIX, path)
                if not default_storage.exists(target_name):
                    # Generate the thumbnail
                    img = Image.open(default_storage.open(path))
                    w, h = width, height = img.size
                    mx = max(width, height)
                    if mx > 60:
                        w = width * 60 // mx
                        h = height * 60 // mx
                    img = img.resize((w, h), Image.ANTIALIAS)
                    ifile = BytesIO()
                    # Thanks, SmileyChris
                    fmt = Image.EXTENSION.get(ext.lower(), u'JPEG')
                    img.save(ifile, fmt)
                    default_storage.save(target_name, File(ifile))
                url = urljoin(settings.settings.MEDIA_URL, default_storage.url(target_name))
            else:
                url = urljoin(settings.settings.MEDIA_URL, default_storage.url(path))
        else:
            # Use generic image for file type, if we have one
            try:
                url = static(u'filemanager/images/icons/{}.png'.format(ext.strip(u'.')))
            except ValueError:
                url = static(u'filemanager/images/icons/default.png')
        return HttpResponseRedirect(url)

    def download(self, path, file_or_dir):
        full_path = safe_join(self.basepath, path)
        base_name = os.path.basename(path)
        if not re.match(ur'[\w\d_ -/]*', path).group(0) == path:
            return HttpResponse(u'Invalid path')
        if file_or_dir == u'file':
            response = HttpResponse(open(full_path), content_type=mimetypes.guess_type(full_path)[0])
            response[u'Content-Length'] = os.path.getsize(full_path)
            response[u'Content-Disposition'] = u'attachment; filename={}'.format(base_name)
            return response
        elif file_or_dir == u'dir':
            response = HttpResponse(content_type=u'application/x-gzip')
            response[u'Content-Disposition'] = u'attachment; filename={}.tar.gz'.format(base_name)
            tarred = tarfile.open(fileobj=response, mode=u'w:gz')
            tarred.add(full_path, arcname=base_name)
            tarred.close()
            return response
