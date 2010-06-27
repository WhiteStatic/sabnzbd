#!/usr/bin/python -OO
# Copyright 2008-2010 The SABnzbd-Team <team@sabnzbd.org>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.

"""
sabnzbd.interface - webinterface
"""

import os
import time
import cherrypy
import logging
import re
import urllib
from xml.sax.saxutils import escape

from sabnzbd.utils.rsslib import RSS, Item
import sabnzbd
import sabnzbd.rss
import sabnzbd.scheduler as scheduler

from Cheetah.Template import Template
import sabnzbd.emailer as emailer
from sabnzbd.misc import real_path, to_units, \
     diskfree, sanitize_foldername, \
     cat_to_opts, int_conv, panic_old_queue, globber
from sabnzbd.newswrapper import GetServerParms
from sabnzbd.newzbin import Bookmarks
from sabnzbd.bpsmeter import BPSMeter
from sabnzbd.encoding import TRANS, xml_name, LatinFilter, unicoder, special_fixer, platform_encode, latin1
import sabnzbd.config as config
import sabnzbd.cfg as cfg
import sabnzbd.newsunpack
from sabnzbd.postproc import PostProcessor
import sabnzbd.downloader as downloader
import sabnzbd.nzbqueue as nzbqueue
import sabnzbd.wizard
from sabnzbd.utils.servertests import test_nntp_server_dict

from sabnzbd.constants import *
from sabnzbd.lang import T, Ta, list_languages, reset_language

from sabnzbd.api import list_scripts, list_cats, del_from_section, \
                        api_handler, build_queue, rss_qstatus, \
                        retry_job, build_header, get_history_size, build_history, \
                        format_bytes, calc_age, std_time, report

#------------------------------------------------------------------------------
# Global constants

DIRECTIVES = {
           'directiveStartToken': '<!--#',
           'directiveEndToken': '#-->',
           'prioritizeSearchListOverSelf' : True
           }
FILTER = LatinFilter

#------------------------------------------------------------------------------
#
def check_server(host, port):
    """ Check if server address resolves properly """

    if host.lower() == 'localhost' and sabnzbd.AMBI_LOCALHOST:
        return badParameterResponse(T('msg-warning-ambiLocalhost'))

    if GetServerParms(host, int_conv(port)):
        return ""
    else:
        return badParameterResponse(T('msg-invalidServer@2') % (host, port))


def ConvertSpecials(p):
    """ Convert None to 'None' and 'Default' to ''
    """
    if p is None:
        p = 'None'
    elif p.lower() == T('default').lower():
        p = ''
    return p


def Raiser(root, **kwargs):
    args = {}
    for key in kwargs:
        val = kwargs.get(key)
        if val:
            args[key] = val
    root = '%s?%s' % (root, urllib.urlencode(args))
    return cherrypy.HTTPRedirect(root)


def queueRaiser(root, kwargs):
    return Raiser(root, start=kwargs.get('start'),
                        limit=kwargs.get('limit'),
                        search=kwargs.get('search'),
                        _dc=kwargs.get('_dc'))

def dcRaiser(root, kwargs):
    return Raiser(root, _dc=kwargs.get('_dc'))


#------------------------------------------------------------------------------
def IsNone(value):
    """ Return True if either None, 'None' or '' """
    return value==None or value=="" or value.lower()=='none'


def Strip(txt):
    """ Return stripped string, can handle None """
    try:
        return txt.strip()
    except:
        return None


#------------------------------------------------------------------------------
# Web login support
def get_users():
    users = {}
    users[cfg.username()] = cfg.password()
    return users

def encrypt_pwd(pwd):
    return pwd


def set_auth(conf):
    """ Set the authentication for CherryPy
    """
    if cfg.username() and cfg.password():
        conf.update({'tools.basic_auth.on' : True, 'tools.basic_auth.realm' : 'SABnzbd',
                            'tools.basic_auth.users' : get_users, 'tools.basic_auth.encrypt' : encrypt_pwd})
        conf.update({'/api':{'tools.basic_auth.on' : False},
                     '/m/api':{'tools.basic_auth.on' : False},
                     '/sabnzbd/api':{'tools.basic_auth.on' : False},
                     '/sabnzbd/m/api':{'tools.basic_auth.on' : False},
                     })
    else:
        conf.update({'tools.basic_auth.on':False})


def check_session(kwargs):
    """ Check session key """
    key = kwargs.get('session')
    if not key:
        key = kwargs.get('apikey')
    msg = None
    if not key:
        logging.warning(Ta('warn-missingKey'))
        msg = T('error-missingKey')
    elif key != cfg.api_key():
        logging.warning(Ta('error-badKey'))
        msg = T('error-badKey')
    return msg


#------------------------------------------------------------------------------
def check_apikey(kwargs, nokey=False):
    """ Check api key """
    output = kwargs.get('output')
    mode = kwargs.get('mode', '')

    # Don't give a visible warning: these commands are used by some
    # external utilities to detect if username/password is required
    special = mode in ('get_scripts', 'qstatus')

    # First check APIKEY, if OK that's sufficient
    if not (cfg.disable_key() or nokey):
        key = kwargs.get('apikey')
        if not key:
            if not special:
                logging.warning(Ta('warn-apikeyNone'))
            return report(output, 'API Key Required')
        elif key != cfg.api_key():
            logging.warning(Ta('warn-apikeyBad'))
            return report(output, 'API Key Incorrect')
        else:
            return None

    # No active APIKEY, check web credentials instead
    if cfg.username() and cfg.password():
        if kwargs.get('ma_username') == cfg.username() and kwargs.get('ma_password') == cfg.password():
            pass
        else:
            if not special:
                logging.warning(Ta('warn-authMissing'))
            return report(output, 'Missing authentication')
    return None


#------------------------------------------------------------------------------
class NoPage(object):
    def __init__(self):
        pass

    @cherrypy.expose
    def index(self, **kwargs):
        return badParameterResponse(T('error-noSecUI'))



class MainPage(object):
    def __init__(self, web_dir, root, web_dir2=None, root2=None, prim=True, first=0):
        self.__root = root
        self.__web_dir = web_dir
        self.__prim = prim
        if first >= 1:
            self.m = MainPage(web_dir2, root2, prim=False)
        if first == 2:
            self.sabnzbd = MainPage(web_dir, '/sabnzbd/', web_dir2, '/sabnzbd/m/', prim=True, first=1)
        self.queue = QueuePage(web_dir, root+'queue/', prim)
        self.history = HistoryPage(web_dir, root+'history/', prim)
        self.connections = ConnectionInfo(web_dir, root+'connections/', prim)
        self.config = ConfigPage(web_dir, root+'config/', prim)
        self.nzb = NzoPage(web_dir, root+'nzb/', prim)
        self.wizard = sabnzbd.wizard.Wizard(web_dir, root+'wizard/', prim)


    @cherrypy.expose
    def index(self, **kwargs):
        if sabnzbd.OLD_QUEUE and not cfg.warned_old_queue():
            cfg.warned_old_queue.set(True)
            config.save_config()
            return panic_old_queue()

        if kwargs.get('skip_wizard') or config.get_servers():
            info, pnfo_list, bytespersec = build_header(self.__prim)

            if cfg.newzbin_username() and cfg.newzbin_password.get_stars():
                info['newzbinDetails'] = True

            info['script_list'] = list_scripts(default=True)
            info['script'] = cfg.dirscan_script()

            info['cat'] = 'Default'
            info['cat_list'] = list_cats(True)

            info['warning'] = ''
            if cfg.enable_unrar():
                if sabnzbd.newsunpack.RAR_PROBLEM and not cfg.ignore_wrong_unrar():
                    info['warning'] = T('warn-badUnrar')
                if not sabnzbd.newsunpack.RAR_COMMAND:
                    info['warning'] = T('warn-noUnpack')
            if not sabnzbd.newsunpack.PAR2_COMMAND:
                info['warning'] = T('warn-noRepair')

            template = Template(file=os.path.join(self.__web_dir, 'main.tmpl'),
                                filter=FILTER, searchList=[info], compilerSettings=DIRECTIVES)
            return template.respond()
        else:
            # Redirect to the setup wizard
            raise cherrypy.HTTPRedirect('/wizard/')

    #@cherrypy.expose
    #def reset_lang(self, **kwargs):
    #    msg = check_session(kwargs)
    #    if msg: return msg
    #    reset_language(cfg.language())
    #    raise dcRaiser(self.__root, kwargs)


    def add_handler(self, kwargs):
        id = kwargs.get('id', '')
        if not id:
            id = kwargs.get('url', '')
        pp = kwargs.get('pp')
        script = kwargs.get('script')
        cat = kwargs.get('cat')
        priority =  kwargs.get('priority')
        redirect = kwargs.get('redirect')
        nzbname = kwargs.get('nzbname')

        RE_NEWZBIN_URL = re.compile(r'/browse/post/(\d+)')
        newzbin_url = RE_NEWZBIN_URL.search(id.lower())

        id = Strip(id)
        if id and (id.isdigit() or len(id)==5):
            sabnzbd.add_msgid(id, pp, script, cat, priority, nzbname)
        elif newzbin_url:
            sabnzbd.add_msgid(Strip(newzbin_url.group(1)), pp, script, cat, priority, nzbname)
        elif id:
            sabnzbd.add_url(id, pp, script, cat, priority, nzbname)
        if not redirect:
            redirect = self.__root
        raise cherrypy.HTTPRedirect(redirect)


    @cherrypy.expose
    def addID(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg
        raise self.add_handler(kwargs)


    @cherrypy.expose
    def addURL(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg
        raise self.add_handler(kwargs)


    @cherrypy.expose
    def addFile(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg

        nzbfile = kwargs.get('nzbfile')
        if nzbfile is not None and nzbfile.filename and nzbfile.value:
            sabnzbd.add_nzbfile(nzbfile, kwargs.get('pp'), kwargs.get('script'),
                                kwargs.get('cat'), kwargs.get('priority', NORMAL_PRIORITY))
        raise dcRaiser(self.__root, kwargs)

    @cherrypy.expose
    def shutdown(self, **kwargs):
        msg = check_session(kwargs)
        if msg:
            yield msg
        else:
            yield "Initiating shutdown..."
            sabnzbd.halt()
            yield "<br>SABnzbd-%s shutdown finished" % sabnzbd.__version__
            cherrypy.engine.exit()
            sabnzbd.SABSTOP = True

    @cherrypy.expose
    def pause(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg

        scheduler.plan_resume(0)
        downloader.pause_downloader()
        raise dcRaiser(self.__root, kwargs)

    @cherrypy.expose
    def resume(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg

        scheduler.plan_resume(0)
        sabnzbd.unpause_all()
        raise dcRaiser(self.__root, kwargs)

    @cherrypy.expose
    def rss(self, **kwargs):
        msg = check_apikey(kwargs, nokey=True)
        if msg: return msg

        if kwargs.get('mode') == 'history':
            return rss_history(cherrypy.url(), limit=kwargs.get('limit',50), search=kwargs.get('search'))
        elif kwargs.get('mode') == 'queue':
            return rss_qstatus()
        elif kwargs.get('mode') == 'warnings':
            return rss_warnings()

    @cherrypy.expose
    def tapi(self, **kwargs):
        """Handler for API over http, for template use
        """
        msg = check_session(kwargs)
        if msg: return msg
        return api_handler(kwargs)

    @cherrypy.expose
    def api(self, **kwargs):
        """Handler for API over http, with explicit authentication parameters
        """
        if kwargs.get('mode', '') not in ('version', 'auth'):
            msg = check_apikey(kwargs)
            if msg: return msg
        return api_handler(kwargs)

    @cherrypy.expose
    def scriptlog(self, **kwargs):
        """ Duplicate of scriptlog of History, needed for some skins """
        # No session key check, due to fixed URLs

        name = kwargs.get('name')
        if name:
            history_db = cherrypy.thread_data.history_db
            return ShowString(history_db.get_name(name), history_db.get_script_log(name))
        else:
            raise dcRaiser(self.__root, kwargs)

    @cherrypy.expose
    def retry(self, **kwargs):
        """ Duplicate of retry of History, needed for some skins """
        msg = check_session(kwargs)
        if msg: return msg

        url = kwargs.get('url', '')
        pp = kwargs.get('pp')
        cat = kwargs.get('cat')
        script = kwargs.get('script')

        url = url.strip()
        if url and (url.isdigit() or len(url)==5):
            sabnzbd.add_msgid(url, pp, script, cat)
        elif url:
            sabnzbd.add_url(url, pp, script, cat)
        if url:
            return ShowOK(url)
        else:
            raise dcRaiser(self.__root, kwargs)

#------------------------------------------------------------------------------
class NzoPage(object):
    def __init__(self, web_dir, root, prim):
        self.__root = root
        self.__web_dir = web_dir
        self.__verbose = False
        self.__prim = prim
        self.__cached_selection = {} #None

    @cherrypy.expose
    def default(self, *args, **kwargs):
        # Allowed URL's
        # /nzb/SABnzbd_nzo_xxxxx/
        # /nzb/SABnzbd_nzo_xxxxx/details
        # /nzb/SABnzbd_nzo_xxxxx/files
        # /nzb/SABnzbd_nzo_xxxxx/bulk_operation
        # /nzb/SABnzbd_nzo_xxxxx/save

        info, pnfo_list, bytespersec = build_header(self.__prim)
        nzo_id = None

        for a in args:
            if a.startswith('SABnzbd_nzo'):
                nzo_id = a
                break

        if nzo_id:
            # /SABnzbd_nzo_xxxxx/bulk_operation
            if 'bulk_operation' in args:
                return self.bulk_operation(nzo_id, kwargs)

            # /SABnzbd_nzo_xxxxx/details
            elif 'details' in args:
                info =  self.nzo_details(info, pnfo_list, nzo_id)

            # /SABnzbd_nzo_xxxxx/files
            elif 'files' in args:
                info =  self.nzo_files(info, pnfo_list, nzo_id)

            # /SABnzbd_nzo_xxxxx/save
            elif 'save' in args:
                self.save_details(nzo_id, args, kwargs)
                return

            # /SABnzbd_nzo_xxxxx/
            else:
                info =  self.nzo_details(info, pnfo_list, nzo_id)
                info =  self.nzo_files(info, pnfo_list, nzo_id)

        template = Template(file=os.path.join(self.__web_dir, 'nzo.tmpl'),
                            filter=FILTER, searchList=[info], compilerSettings=DIRECTIVES)
        return template.respond()


    def nzo_details(self, info, pnfo_list, nzo_id):
        slot = {}
        n = 0
        for pnfo in pnfo_list:
            if pnfo[PNFO_NZO_ID_FIELD] == nzo_id:
                repair = pnfo[PNFO_REPAIR_FIELD]
                unpack = pnfo[PNFO_UNPACK_FIELD]
                delete = pnfo[PNFO_DELETE_FIELD]
                unpackopts = sabnzbd.opts_to_pp(repair, unpack, delete)
                script = pnfo[PNFO_SCRIPT_FIELD]
                if script is None:
                    script = 'None'
                cat = pnfo[PNFO_EXTRA_FIELD1]
                if not cat:
                    cat = 'None'
                filename = xml_name(pnfo[PNFO_FILENAME_FIELD])
                priority = pnfo[PNFO_PRIORITY_FIELD]

                slot['nzo_id'] =  str(nzo_id)
                slot['cat'] = cat
                slot['filename'] = filename
                slot['script'] = script
                slot['priority'] = str(priority)
                slot['unpackopts'] = str(unpackopts)
                info['index'] = n
                break
            n += 1

        info['slot'] = slot
        info['script_list'] = list_scripts()
        info['cat_list'] = list_cats()
        info['noofslots'] = len(pnfo_list)

        return info

    def nzo_files(self, info, pnfo_list, nzo_id):

        active = []
        for pnfo in pnfo_list:
            if pnfo[PNFO_NZO_ID_FIELD] == nzo_id:
                info['nzo_id'] = nzo_id
                info['filename'] = xml_name(pnfo[PNFO_FILENAME_FIELD])

                for tup in pnfo[PNFO_ACTIVE_FILES_FIELD]:
                    bytes_left, bytes, fn, date, nzf_id = tup
                    checked = False
                    if nzf_id in self.__cached_selection and \
                       self.__cached_selection[nzf_id] == 'on':
                        checked = True

                    line = {'filename':xml_name(fn),
                            'mbleft':"%.2f" % (bytes_left / MEBI),
                            'mb':"%.2f" % (bytes / MEBI),
                            'size': format_bytes(bytes),
                            'sizeleft':format_bytes(bytes_left),
                            'nzf_id':nzf_id,
                            'age':calc_age(date),
                            'checked':checked}
                    active.append(line)
                break

        info['active_files'] = active
        return info


    def save_details(self, nzo_id, args, kwargs):
        index = kwargs.get('index', None)
        name = kwargs.get('name', None)
        pp = kwargs.get('pp', None)
        script = kwargs.get('script', None)
        cat = kwargs.get('cat', None)
        priority = kwargs.get('priority', None)
        nzo = sabnzbd.nzbqueue.get_nzo(nzo_id)

        if index != None:
            nzbqueue.switch(nzo_id, index)
        if name != None:
            sabnzbd.nzbqueue.change_name(nzo_id, special_fixer(name))
        if cat != None:
            sabnzbd.nzbqueue.change_cat(nzo_id,cat)
        if script != None:
            sabnzbd.nzbqueue.change_script(nzo_id,script)
        if pp != None:
            sabnzbd.nzbqueue.change_opts(nzo_id,pp)
        if priority != None and nzo and nzo.priority != int(priority):
            sabnzbd.nzbqueue.set_priority(nzo_id, priority)

        args = [arg for arg in args if arg != 'save']
        extra = '/'.join(args)
        url = cherrypy._urljoin(self.__root, extra)
        if url and not url.endswith('/'):
            url += '/'
        raise dcRaiser(url, {})

    def bulk_operation(self, nzo_id, kwargs):
        self.__cached_selection = kwargs
        if kwargs['action_key'] == 'Delete':
            for key in kwargs:
                if kwargs[key] == 'on':
                    nzbqueue.remove_nzf(nzo_id, key)

        elif kwargs['action_key'] == 'Top' or kwargs['action_key'] == 'Up' or \
             kwargs['action_key'] == 'Down' or kwargs['action_key'] == 'Bottom':
            nzf_ids = []
            for key in kwargs:
                if kwargs[key] == 'on':
                    nzf_ids.append(key)
            if kwargs['action_key'] == 'Top':
                nzbqueue.move_top_bulk(nzo_id, nzf_ids)
            elif kwargs['action_key'] == 'Up':
                nzbqueue.move_up_bulk(nzo_id, nzf_ids)
            elif kwargs['action_key'] == 'Down':
                nzbqueue.move_down_bulk(nzo_id, nzf_ids)
            elif kwargs['action_key'] == 'Bottom':
                nzbqueue.move_bottom_bulk(nzo_id, nzf_ids)

        if nzbqueue.get_nzo(nzo_id):
            url = cherrypy._urljoin(self.__root, nzo_id)
        else:
            url = cherrypy._urljoin(self.__root, '../queue')
        if url and not url.endswith('/'):
            url += '/'
        raise dcRaiser(url, kwargs)

#------------------------------------------------------------------------------
class QueuePage(object):
    def __init__(self, web_dir, root, prim):
        self.__root = root
        self.__web_dir = web_dir
        self.__verbose = False
        self.__verboseList = []
        self.__prim = prim

    @cherrypy.expose
    def index(self, **kwargs):
        start = kwargs.get('start')
        limit = kwargs.get('limit')
        dummy2 = kwargs.get('dummy2')

        info, pnfo_list, bytespersec, self.__verboseList, self.__dict__ = build_queue(self.__web_dir, self.__root, self.__verbose, self.__prim, self.__verboseList, self.__dict__, start=start, limit=limit, dummy2=dummy2)

        template = Template(file=os.path.join(self.__web_dir, 'queue.tmpl'),
                            filter=FILTER, searchList=[info], compilerSettings=DIRECTIVES)
        return template.respond()



    @cherrypy.expose
    def delete(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg
        uid = kwargs.get('uid')
        if uid:
            nzbqueue.remove_nzo(uid, False)
        raise queueRaiser(self.__root, kwargs)

    @cherrypy.expose
    def purge(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg
        nzbqueue.remove_all_nzo()
        raise queueRaiser(self.__root, kwargs)

    @cherrypy.expose
    def removeNzf(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg
        nzo_id = kwargs.get('nzo_id')
        nzf_id = kwargs.get('nzf_id')
        if nzo_id and nzf_id:
            nzbqueue.remove_nzf(nzo_id, nzf_id)
        raise queueRaiser(self.__root, kwargs)

    @cherrypy.expose
    def tog_verbose(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg
        self.__verbose = not self.__verbose
        raise queueRaiser(self.__root, kwargs)

    @cherrypy.expose
    def tog_uid_verbose(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg
        uid = kwargs.get('uid')
        if self.__verboseList.count(uid):
            self.__verboseList.remove(uid)
        else:
            self.__verboseList.append(uid)
        raise queueRaiser(self.__root, kwargs)

    @cherrypy.expose
    def change_queue_complete_action(self, **kwargs):
        """
        Action or script to be performed once the queue has been completed
        Scripts are prefixed with 'script_'
        """
        msg = check_session(kwargs)
        if msg: return msg
        action = kwargs.get('action')
        sabnzbd.change_queue_complete_action(action)
        cfg.queue_complete.set(action)
        config.save_config()
        raise queueRaiser(self.__root, kwargs)

    @cherrypy.expose
    def switch(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg
        uid1 = kwargs.get('uid1')
        uid2 = kwargs.get('uid2')
        if uid1 and uid2:
            nzbqueue.switch(uid1, uid2)
        raise queueRaiser(self.__root, kwargs)

    @cherrypy.expose
    def change_opts(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg
        nzo_id = kwargs.get('nzo_id')
        pp = kwargs.get('pp', '')
        if nzo_id and pp and pp.isdigit():
            nzbqueue.change_opts(nzo_id, int(pp))
        raise queueRaiser(self.__root, kwargs)

    @cherrypy.expose
    def change_script(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg
        nzo_id = kwargs.get('nzo_id')
        script = kwargs.get('script', '')
        if nzo_id and script:
            if script == 'None':
                script = None
            nzbqueue.change_script(nzo_id, script)
        raise queueRaiser(self.__root, kwargs)

    @cherrypy.expose
    def change_cat(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg
        nzo_id = kwargs.get('nzo_id')
        cat = kwargs.get('cat', '')
        if nzo_id and cat:
            if cat == 'None':
                cat = None
            nzbqueue.change_cat(nzo_id, cat)
            item = config.get_config('categories', cat)
            if item:
                cat, pp, script, priority = cat_to_opts(cat)
            else:
                script = cfg.dirscan_script()
                pp = cfg.dirscan_pp()
                priority = cfg.dirscan_priority()

            nzbqueue.change_script(nzo_id, script)
            nzbqueue.change_opts(nzo_id, pp)
            nzbqueue.set_priority(nzo_id, priority)

        raise queueRaiser(self.__root, kwargs)

    @cherrypy.expose
    def shutdown(self, **kwargs):
        msg = check_session(kwargs)
        if msg:
            yield msg
        else:
            yield "Initiating shutdown..."
            sabnzbd.halt()
            cherrypy.engine.exit()
            yield "<br>SABnzbd-%s shutdown finished" % sabnzbd.__version__
            sabnzbd.SABSTOP = True

    @cherrypy.expose
    def pause(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg
        scheduler.plan_resume(0)
        downloader.pause_downloader()
        raise queueRaiser(self.__root, kwargs)

    @cherrypy.expose
    def resume(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg
        scheduler.plan_resume(0)
        sabnzbd.unpause_all()
        raise queueRaiser(self.__root, kwargs)

    @cherrypy.expose
    def pause_nzo(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg
        uid = kwargs.get('uid', '')
        nzbqueue.pause_multiple_nzo(uid.split(','))
        raise queueRaiser(self.__root, kwargs)

    @cherrypy.expose
    def resume_nzo(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg
        uid = kwargs.get('uid', '')
        nzbqueue.resume_multiple_nzo(uid.split(','))
        raise queueRaiser(self.__root, kwargs)

    @cherrypy.expose
    def set_priority(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg
        nzbqueue.set_priority(kwargs.get('nzo_id'), kwargs.get('priority'))
        raise queueRaiser(self.__root, kwargs)

    @cherrypy.expose
    def sort_by_avg_age(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg
        nzbqueue.sort_queue('avg_age', kwargs.get('dir'))
        raise queueRaiser(self.__root, kwargs)

    @cherrypy.expose
    def sort_by_name(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg
        nzbqueue.sort_queue('name', kwargs.get('dir'))
        raise queueRaiser(self.__root, kwargs)

    @cherrypy.expose
    def sort_by_size(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg
        nzbqueue.sort_queue('size', kwargs.get('dir'))
        raise queueRaiser(self.__root, kwargs)

    @cherrypy.expose
    def set_speedlimit(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg
        downloader.limit_speed(int_conv(kwargs.get('value')))
        raise dcRaiser(self.__root, kwargs)

    @cherrypy.expose
    def set_pause(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg
        scheduler.plan_resume(int_conv(kwargs.get('value')))
        raise dcRaiser(self.__root, kwargs)

class HistoryPage(object):
    def __init__(self, web_dir, root, prim):
        self.__root = root
        self.__web_dir = web_dir
        self.__verbose = False
        self.__verbose_list = []
        self.__prim = prim

    @cherrypy.expose
    def index(self, **kwargs):
        start = kwargs.get('start')
        limit = kwargs.get('limit')
        search = kwargs.get('search')

        history, pnfo_list, bytespersec = build_header(self.__prim)

        history['isverbose'] = self.__verbose

        if cfg.newzbin_username() and cfg.newzbin_password():
            history['newzbinDetails'] = True

        #history_items, total_bytes, bytes_beginning = sabnzbd.history_info()
        #history['bytes_beginning'] = "%.2f" % (bytes_beginning / GIGI)

        grand, month, week, day = BPSMeter.do.get_sums()
        history['total_size'], history['month_size'], history['week_size'], history['day_size'] = \
                to_units(grand), to_units(month), to_units(week), to_units(day)

        history['lines'], history['fetched'], history['noofslots'] = build_history(limit=limit, start=start, verbose=self.__verbose, verbose_list=self.__verbose_list, search=search)

        if search:
            history['search'] = escape(search)
        else:
            history['search'] = ''

        history['start'] = int_conv(start)
        history['limit'] = int_conv(limit)
        history['finish'] = history['start'] + history['limit']
        if history['finish'] > history['noofslots']:
            history['finish'] = history['noofslots']
        if not history['finish']:
            history['finish'] = history['fetched']


        template = Template(file=os.path.join(self.__web_dir, 'history.tmpl'),
                            filter=FILTER, searchList=[history], compilerSettings=DIRECTIVES)
        return template.respond()

    @cherrypy.expose
    def purge(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg
        history_db = cherrypy.thread_data.history_db
        history_db.remove_history()
        raise queueRaiser(self.__root, kwargs)

    @cherrypy.expose
    def delete(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg
        job = kwargs.get('job')
        if job:
            history_db = cherrypy.thread_data.history_db
            jobs = job.split(',')
            for job in jobs:
                PostProcessor.do.delete(job)
                history_db.remove_history(job)
        raise queueRaiser(self.__root, kwargs)

    @cherrypy.expose
    def retry_pp(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg
        retry_job(kwargs.get('job'), kwargs.get('nzbfile'))
        raise queueRaiser(self.__root, kwargs)

    @cherrypy.expose
    def purge_failed(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg
        history_db = cherrypy.thread_data.history_db
        history_db.remove_failed()
        raise queueRaiser(self.__root, kwargs)

    @cherrypy.expose
    def reset(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg
        #sabnzbd.reset_byte_counter()
        raise queueRaiser(self.__root, kwargs)

    @cherrypy.expose
    def tog_verbose(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg
        jobs = kwargs.get('jobs')
        if not jobs:
            self.__verbose = not self.__verbose
            self.__verbose_list = []
        else:
            if self.__verbose:
                self.__verbose = False
            else:
                jobs = jobs.split(',')
                for job in jobs:
                    if job in self.__verbose_list:
                        self.__verbose_list.remove(job)
                    else:
                        self.__verbose_list.append(job)
        raise queueRaiser(self.__root, kwargs)

    @cherrypy.expose
    def scriptlog(self, **kwargs):
        """ Duplicate of scriptlog of History, needed for some skins """
        # No session key check, due to fixed URLs

        name = kwargs.get('name')
        if name:
            history_db = cherrypy.thread_data.history_db
            return ShowString(history_db.get_name(name), history_db.get_script_log(name))
        else:
            raise dcRaiser(self.__root, kwargs)

    @cherrypy.expose
    def retry(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg
        url = kwargs.get('url', '').strip()
        pp = kwargs.get('pp')
        cat = kwargs.get('cat')
        script = kwargs.get('script')
        if url and (url.isdigit() or len(url)==5):
            sabnzbd.add_msgid(url, pp, script, cat)
        elif url:
            sabnzbd.add_url(url, pp, script, cat, nzbname=kwargs.get('nzbname'))
        if url:
            return ShowOK(url)
        else:
            raise dcRaiser(self.__root, kwargs)

#------------------------------------------------------------------------------
class ConfigPage(object):
    def __init__(self, web_dir, root, prim):
        self.__root = root
        self.__web_dir = web_dir
        self.__prim = prim
        self.directories = ConfigDirectories(web_dir, root+'directories/', prim)
        self.email = ConfigEmail(web_dir, root+'email/', prim)
        self.general = ConfigGeneral(web_dir, root+'general/', prim)
        self.newzbin = ConfigNewzbin(web_dir, root+'newzbin/', prim)
        self.rss = ConfigRss(web_dir, root+'rss/', prim)
        self.scheduling = ConfigScheduling(web_dir, root+'scheduling/', prim)
        self.server = ConfigServer(web_dir, root+'server/', prim)
        self.switches = ConfigSwitches(web_dir, root+'switches/', prim)
        self.categories = ConfigCats(web_dir, root+'categories/', prim)
        self.sorting = ConfigSorting(web_dir, root+'sorting/', prim)


    @cherrypy.expose
    def index(self, **kwargs):
        conf, pnfo_list, bytespersec = build_header(self.__prim)

        conf['configfn'] = config.get_filename()

        new = {}
        for svr in config.get_servers():
            new[svr] = {}
        conf['servers'] = new

        template = Template(file=os.path.join(self.__web_dir, 'config.tmpl'),
                            filter=FILTER, searchList=[conf], compilerSettings=DIRECTIVES)
        return template.respond()

    @cherrypy.expose
    def restart(self, **kwargs):
        msg = check_session(kwargs)
        if msg:
            yield msg
        else:
            yield T('restart1')
            sabnzbd.halt()
            yield T('restart2')
            cherrypy.engine.restart()

    @cherrypy.expose
    def repair(self, **kwargs):
        msg = check_session(kwargs)
        if msg:
            yield msg
        else:
            sabnzbd.request_repair()
            yield T('restart1')
            sabnzbd.halt()
            yield T('restart2')
            cherrypy.engine.restart()

    @cherrypy.expose
    def scan(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg
        nzbqueue.scan_jobs()
        raise dcRaiser(self.__root, kwargs)

#------------------------------------------------------------------------------
LIST_DIRPAGE = ( \
    'download_dir', 'download_free', 'complete_dir', 'cache_dir', 'admin_dir',
    'nzb_backup_dir', 'dirscan_dir', 'dirscan_speed', 'script_dir',
    'email_dir', 'permissions', 'log_dir'
    )

class ConfigDirectories(object):
    def __init__(self, web_dir, root, prim):
        self.__root = root
        self.__web_dir = web_dir
        self.__prim = prim

    @cherrypy.expose
    def index(self, **kwargs):
        if cfg.configlock():
            return Protected()

        conf, pnfo_list, bytespersec = build_header(self.__prim)

        for kw in LIST_DIRPAGE:
            conf[kw] = config.get_config('misc', kw)()

        conf['my_home'] = sabnzbd.DIR_HOME
        conf['my_lcldata'] = sabnzbd.DIR_LCLDATA

        # Temporary fix, problem with build_header
        conf['restart_req'] = sabnzbd.RESTART_REQ

        template = Template(file=os.path.join(self.__web_dir, 'config_directories.tmpl'),
                            filter=FILTER, searchList=[conf], compilerSettings=DIRECTIVES)
        return template.respond()

    @cherrypy.expose
    def saveDirectories(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg

        cfg.complete_dir.set_create()
        for kw in LIST_DIRPAGE:
            value = kwargs.get(kw)
            if value != None:
                value = platform_encode(value)
                msg = config.get_config('misc', kw).set(value)
                if msg:
                    return badParameterResponse(msg)

        config.save_config()
        raise dcRaiser(self.__root, kwargs)


SWITCH_LIST = \
    ('par2_multicore', 'par_option', 'enable_unrar', 'enable_unzip', 'enable_filejoin',
     'enable_tsjoin', 'send_group', 'fail_on_crc', 'top_only',
     'dirscan_opts', 'enable_par_cleanup', 'auto_sort', 'check_new_rel', 'auto_disconnect',
     'safe_postproc', 'no_dupes', 'replace_spaces', 'replace_illegal', 'auto_browser',
     'ignore_samples', 'pause_on_post_processing', 'quick_check', 'dirscan_script', 'nice', 'ionice',
     'dirscan_priority', 'ssl_type', 'pre_script', 'pause_on_pwrar'
    )

#------------------------------------------------------------------------------
class ConfigSwitches(object):
    def __init__(self, web_dir, root, prim):
        self.__root = root
        self.__web_dir = web_dir
        self.__prim = prim

    @cherrypy.expose
    def index(self, **kwargs):
        if cfg.configlock():
            return Protected()

        conf, pnfo_list, bytespersec = build_header(self.__prim)

        conf['nt'] = sabnzbd.WIN32
        conf['have_nice'] = bool(sabnzbd.newsunpack.NICE_COMMAND)
        conf['have_ionice'] = bool(sabnzbd.newsunpack.IONICE_COMMAND)

        for kw in SWITCH_LIST:
            conf[kw] = config.get_config('misc', kw)()

        conf['script_list'] = list_scripts()

        template = Template(file=os.path.join(self.__web_dir, 'config_switches.tmpl'),
                            filter=FILTER, searchList=[conf], compilerSettings=DIRECTIVES)
        return template.respond()

    @cherrypy.expose
    def saveSwitches(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg

        for kw in SWITCH_LIST:
            item = config.get_config('misc', kw)
            value = platform_encode(kwargs.get(kw))
            msg = item.set(value)
            if msg:
                return badParameterResponse(msg)

        config.save_config()
        raise dcRaiser(self.__root, kwargs)


#------------------------------------------------------------------------------
GENERAL_LIST = (
    'host', 'port', 'username', 'password', 'disable_api_key',
    'refresh_rate', 'rss_rate',
    'cache_limit',
    'enable_https', 'https_port', 'https_cert', 'https_key'
    )

class ConfigGeneral(object):
    def __init__(self, web_dir, root, prim):
        self.__root = root
        self.__web_dir = web_dir
        self.__prim = prim

    @cherrypy.expose
    def index(self, **kwargs):
        def ListColors(web_dir):
            lst = []
            web_dir = os.path.join(sabnzbd.DIR_INTERFACES, web_dir)
            dd = os.path.abspath(web_dir + '/templates/static/stylesheets/colorschemes')
            if (not dd) or (not os.access(dd, os.R_OK)):
                return lst
            for color in globber(dd):
                col = os.path.basename(color).replace('.css','')
                if col != "_svn" and col != ".svn":
                    lst.append(col)
            return lst

        def add_color(dir, color):
            if dir:
                if not color:
                    try:
                        color = DEF_SKIN_COLORS[dir.lower()]
                    except KeyError:
                        return dir
                return '%s - %s' % (dir, color)
            else:
                return ''

        if cfg.configlock():
            return Protected()

        conf, pnfo_list, bytespersec = build_header(self.__prim)

        conf['configfn'] = config.get_filename()

        # Temporary fix, problem with build_header
        conf['restart_req'] = sabnzbd.RESTART_REQ

        if sabnzbd.newswrapper.HAVE_SSL:
            conf['have_ssl'] = 1
        else:
            conf['have_ssl'] = 0

        wlist = []
        wlist2 = ['None']
        interfaces = globber(sabnzbd.DIR_INTERFACES)
        for k in interfaces:
            if k.endswith(DEF_STDINTF):
                interfaces.remove(k)
                interfaces.insert(0, k)
                break
        for web in interfaces:
            rweb = os.path.basename(web)
            if rweb != '.svn' and rweb != '_svn' and os.access(web + '/' + DEF_MAIN_TMPL, os.R_OK):
                cols = ListColors(rweb)
                if cols:
                    for col in cols:
                        if rweb != 'Mobile':
                            wlist.append(add_color(rweb, col))
                        wlist2.append(add_color(rweb, col))
                else:
                    if rweb != 'Mobile':
                        wlist.append(rweb)
                    wlist2.append(rweb)
        conf['web_list'] = wlist
        conf['web_list2'] = wlist2

        # Obsolete template variables, must exist and have a value
        conf['web_colors'] = ['None']
        conf['web_color'] = 'None'
        conf['web_colors2'] = ['None']
        conf['web_color2'] = 'None'

        conf['web_dir']  = add_color(cfg.web_dir(), cfg.web_color())
        conf['web_dir2'] = add_color(cfg.web_dir2(), cfg.web_color2())

        conf['language'] = cfg.language()
        list = list_languages(sabnzbd.DIR_LANGUAGE)
        if len(list) < 2:
            list = []
        conf['lang_list'] = list

        conf['disable_api_key'] = cfg.disable_key()
        conf['host'] = cfg.cherryhost()
        conf['port'] = cfg.cherryport()
        conf['https_port'] = cfg.https_port()
        conf['https_cert'] = cfg.https_cert()
        conf['https_key'] = cfg.https_key()
        conf['enable_https'] = cfg.enable_https()
        conf['username'] = cfg.username()
        conf['password'] = cfg.password.get_stars()
        conf['bandwidth_limit'] = cfg.bandwidth_limit()
        conf['refresh_rate'] = cfg.refresh_rate()
        conf['rss_rate'] = cfg.rss_rate()
        conf['cache_limit'] = cfg.cache_limit()
        conf['cleanup_list'] = cfg.cleanup_list.get_string()

        template = Template(file=os.path.join(self.__web_dir, 'config_general.tmpl'),
                            filter=FILTER, searchList=[conf], compilerSettings=DIRECTIVES)
        return template.respond()

    @cherrypy.expose
    def saveGeneral(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg

        # Special handling for cache_limitstr
        #kwargs['cache_limit'] = kwargs.get('cache_limitstr')

        # Handle general options
        for kw in GENERAL_LIST:
            item = config.get_config('misc', kw)
            value = platform_encode(kwargs.get(kw))
            msg = item.set(value)
            if msg:
                return badParameterResponse(msg)

        # Handle special options
        language = kwargs.get('language')
        if language and language != cfg.language():
            cfg.language.set(language)
            reset_language(language)

        cleanup_list = kwargs.get('cleanup_list')
        if cleanup_list and sabnzbd.WIN32:
            cleanup_list = cleanup_list.lower()
        cfg.cleanup_list.set(cleanup_list)

        web_dir = kwargs.get('web_dir')
        web_dir2 = kwargs.get('web_dir2')
        change_web_dir(web_dir)
        try:
            web_dir2, web_color2 = web_dir2.split(' - ')
        except:
            web_color2 = ''
        web_dir2_path = real_path(sabnzbd.DIR_INTERFACES, web_dir2)

        if web_dir2 == 'None':
            cfg.web_dir2.set('')
        elif os.path.exists(web_dir2_path):
            cfg.web_dir2.set(web_dir2)
        cfg.web_color2.set(web_color2)

        bandwidth_limit = kwargs.get('bandwidth_limit')
        if bandwidth_limit != None:
            bandwidth_limit = int_conv(bandwidth_limit)
            cfg.bandwidth_limit.set(bandwidth_limit)

        config.save_config()

        # Update CherryPy authentication
        set_auth(cherrypy.config)
        raise dcRaiser(self.__root, kwargs)

    @cherrypy.expose
    def generateAPIKey(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg

        logging.debug('API Key Changed')
        cfg.api_key.set(config.create_api_key())
        config.save_config()
        raise dcRaiser(self.__root, kwargs)

def change_web_dir(web_dir):
    try:
        web_dir, web_color = web_dir.split(' - ')
    except:
        try:
            web_color = DEF_SKIN_COLORS[web_dir.lower()]
        except:
            web_color = ''

    web_dir_path = real_path(sabnzbd.DIR_INTERFACES, web_dir)

    if not os.path.exists(web_dir_path):
        return badParameterResponse('Cannot find web template: %s' % unicoder(web_dir_path))
    else:
        cfg.web_dir.set(web_dir)
        cfg.web_color.set(web_color)


#------------------------------------------------------------------------------

class ConfigServer(object):
    def __init__(self, web_dir, root, prim):
        self.__root = root
        self.__web_dir = web_dir
        self.__prim = prim

    @cherrypy.expose
    def index(self, **kwargs):
        if cfg.configlock():
            return Protected()

        conf, pnfo_list, bytespersec = build_header(self.__prim)

        new = {}
        servers = config.get_servers()
        for svr in servers:
            new[svr] = servers[svr].get_dict(safe=True)
            t, m, w, d = BPSMeter.do.amounts(svr)
            if t:
                new[svr]['amounts'] = to_units(t), to_units(m), to_units(w), to_units(d)
        conf['servers'] = new

        if sabnzbd.newswrapper.HAVE_SSL:
            conf['have_ssl'] = 1
        else:
            conf['have_ssl'] = 0

        template = Template(file=os.path.join(self.__web_dir, 'config_server.tmpl'),
                            filter=FILTER, searchList=[conf], compilerSettings=DIRECTIVES)
        return template.respond()


    @cherrypy.expose
    def addServer(self, **kwargs):
        return handle_server(kwargs, self.__root)


    @cherrypy.expose
    def saveServer(self, **kwargs):
        return handle_server(kwargs, self.__root)

    @cherrypy.expose
    def testServer(self, **kwargs):
        return handle_server_test(kwargs, self.__root)


    @cherrypy.expose
    def delServer(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg
        kwargs['section'] = 'servers'
        kwargs['keyword'] = kwargs.get('server')
        del_from_section(kwargs)
        raise dcRaiser(self.__root, kwargs)

def handle_server(kwargs, root=None):
    """ Internal server handler """
    msg = check_session(kwargs)
    if msg: return msg

    host = kwargs.get('host', '').strip()
    if not host:
        return badParameterResponse(T('error-needServer'))

    port = kwargs.get('port', '').strip()
    if not port:
        if not kwargs.get('ssl', '').strip():
            port = '119'
        else:
            port = '563'
        kwargs['port'] = port

    if kwargs.get('connections', '').strip() == '':
        kwargs['connections'] = '1'

    msg = check_server(host, port)
    if msg:
        return msg

    server = '%s:%s' % (host, port)

    svr = None
    old_server = kwargs.get('server')
    if old_server:
        svr = config.get_config('servers', old_server)
    if not svr:
        svr = config.get_config('servers', server)

    if svr:
        for kw in ('fillserver', 'ssl', 'enable', 'optional'):
            if kw not in kwargs.keys():
                kwargs[kw] = None
        svr.set_dict(kwargs)
        svr.rename(server)
    else:
        old_server = None
        config.ConfigServer(server, kwargs)

    config.save_config()
    downloader.update_server(old_server, server)
    if root:
        raise dcRaiser(root, kwargs)


def handle_server_test(kwargs, root):
    result, msg = test_nntp_server_dict(kwargs)
    return msg

#------------------------------------------------------------------------------

class ConfigRss(object):
    def __init__(self, web_dir, root, prim):
        self.__root = root
        self.__web_dir = web_dir
        self.__prim = prim

    @cherrypy.expose
    def index(self, **kwargs):
        if cfg.configlock():
            return Protected()

        conf, pnfo_list, bytespersec = build_header(self.__prim)

        conf['script_list'] = list_scripts(default=True)
        pick_script = conf['script_list'] != []

        conf['cat_list'] = list_cats(default=True)
        pick_cat = conf['cat_list'] != []

        rss = {}
        feeds = config.get_rss()
        for feed in feeds:
            rss[feed] = feeds[feed].get_dict()
            filters = feeds[feed].filters()
            rss[feed]['filters'] = filters
            rss[feed]['filtercount'] = len(filters)

            rss[feed]['pick_cat'] = pick_cat
            rss[feed]['pick_script'] = pick_script

        conf['rss'] = rss

        # Find a unique new Feed name
        unum = 1
        while 'Feed'+str(unum) in feeds:
            unum += 1
        conf['feed'] = 'Feed' + str(unum)

        template = Template(file=os.path.join(self.__web_dir, 'config_rss.tmpl'),
                            filter=FILTER, searchList=[conf], compilerSettings=DIRECTIVES)
        return template.respond()

    @cherrypy.expose
    def upd_rss_feed(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg
        if kwargs.get('enable') is not None:
            del kwargs['enable']
        try:
            cfg = config.get_rss()[kwargs.get('feed')]
        except KeyError:
            cfg = None
        if cfg and Strip(kwargs.get('uri')):
            cfg.set_dict(kwargs)
            config.save_config()

        raise dcRaiser(self.__root, kwargs)

    @cherrypy.expose
    def toggle_rss_feed(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg
        try:
            cfg = config.get_rss()[kwargs.get('feed')]
        except KeyError:
            cfg = None
        if cfg:
            cfg.enable.set(not cfg.enable())
            config.save_config()
        raise dcRaiser(self.__root, kwargs)

    @cherrypy.expose
    def add_rss_feed(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg
        feed= Strip(kwargs.get('feed'))
        uri = Strip(kwargs.get('uri'))
        try:
            cfg = config.get_rss()[feed]
        except KeyError:
            cfg = None
        if (not cfg) and uri:
            config.ConfigRSS(feed, kwargs)
            # Clear out any existing reference to this feed name
            # Otherwise first-run detection can fail
            sabnzbd.rss.clear_feed(feed)
            config.save_config()

        raise dcRaiser(self.__root, kwargs)

    @cherrypy.expose
    def upd_rss_filter(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg
        try:
            cfg = config.get_rss()[kwargs.get('feed')]
        except KeyError:
            raise dcRaiser(self.__root, kwargs)

        pp = kwargs.get('pp')
        if IsNone(pp): pp = ''
        script = ConvertSpecials(kwargs.get('script'))
        cat = ConvertSpecials(kwargs.get('cat'))

        cfg.filters.update(int(kwargs.get('index', 0)), (cat, pp, script, kwargs.get('filter_type'), \
                           platform_encode(kwargs.get('filter_text'))))
        config.save_config()
        raise dcRaiser(self.__root, kwargs)

    @cherrypy.expose
    def pos_rss_filter(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg
        feed = kwargs.get('feed')
        current = kwargs.get('current', 0)
        new = kwargs.get('new', 0)

        try:
            cfg = config.get_rss()[feed]
        except KeyError:
            raise dcRaiser(self.__root, kwargs)

        if current != new:
            cfg.filters.move(int(current), int(new))
            config.save_config()
        raise dcRaiser(self.__root, kwargs)

    @cherrypy.expose
    def del_rss_feed(self, *args, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg
        kwargs['section'] = 'rss'
        kwargs['keyword'] = kwargs.get('feed')
        del_from_section(kwargs)
        raise dcRaiser(self.__root, kwargs)

    @cherrypy.expose
    def del_rss_filter(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg
        try:
            cfg = config.get_rss()[kwargs.get('feed')]
        except KeyError:
            raise dcRaiser(self.__root, kwargs)

        cfg.filters.delete(int(kwargs.get('index', 0)))
        config.save_config()
        raise dcRaiser(self.__root, kwargs)

    @cherrypy.expose
    def download_rss_feed(self, *args, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg
        if 'feed' in kwargs:
            feed = kwargs['feed']
            msg = sabnzbd.rss.run_feed(feed, download=True, force=True)
            if msg:
                return badParameterResponse(msg)
            else:
                return ShowRssLog(feed, False)
        raise dcRaiser(self.__root, kwargs)

    @cherrypy.expose
    def test_rss_feed(self, *args, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg
        if 'feed' in kwargs:
            feed = kwargs['feed']
            msg = sabnzbd.rss.run_feed(feed, download=False, ignoreFirst=True)
            if msg:
                return badParameterResponse(msg)
            else:
                return ShowRssLog(feed, True)
        raise dcRaiser(self.__root, kwargs)


    @cherrypy.expose
    def rss_download(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg
        feed = kwargs.get('feed')
        id = kwargs.get('id')
        cat = kwargs.get('cat')
        pp = kwargs.get('pp')
        script = kwargs.get('script')
        priority = kwargs.get('priority', NORMAL_PRIORITY)
        nzbname = kwargs.get('nzbname')
        if id and id.isdigit():
            sabnzbd.add_msgid(id, pp, script, cat, priority, nzbname)
        elif id:
            sabnzbd.add_url(id, pp, script, cat, priority, nzbname)
        # Need to pass the title instead
        sabnzbd.rss.flag_downloaded(feed, id)
        raise dcRaiser(self.__root, kwargs)


#------------------------------------------------------------------------------
_SCHED_ACTIONS = ('resume', 'pause', 'pause_all', 'shutdown', 'restart', 'speedlimit',
                  'pause_post', 'resume_post', 'scan_folder')

class ConfigScheduling(object):
    def __init__(self, web_dir, root, prim):
        self.__root = root
        self.__web_dir = web_dir
        self.__prim = prim

    @cherrypy.expose
    def index(self, **kwargs):
        def get_days():
            days = {}
            days["*"] = T('daily')
            days["1"] = T('monday')
            days["2"] = T('tuesday')
            days["3"] = T('wednesday')
            days["4"] = T('thursday')
            days["5"] = T('friday')
            days["6"] = T('saturday')
            days["7"] = T('sunday')
            return days

        if cfg.configlock():
            return Protected()

        conf, pnfo_list, bytespersec = build_header(self.__prim)

        actions = []
        actions.extend(_SCHED_ACTIONS)
        days = get_days()
        conf['schedlines'] = []
        snum = 1
        conf['taskinfo'] = []
        for ev in scheduler.sort_schedules(forward=True):
            line = ev[3]
            conf['schedlines'].append(line)
            try:
                m, h, day, action = line.split(' ', 3)
            except:
                continue
            action = action.strip()
            if action in actions:
                action = T("sch-" + action)
            else:
                try:
                    act, server = action.split()
                except ValueError:
                    act = ''
                if act in ('enable_server', 'disable_server'):
                    action = T("sch-" + act) + ' ' + server
            item = (snum, h, '%02d' % int(m), days.get(day, '**'), action)
            conf['taskinfo'].append(item)
            snum += 1


        actions_lng = {}
        for action in actions:
            actions_lng[action] = T("sch-" + action)
        for server in config.get_servers():
            actions.append(server)
            actions_lng[server] = server
        conf['actions'] = actions
        conf['actions_lng'] = actions_lng

        template = Template(file=os.path.join(self.__web_dir, 'config_scheduling.tmpl'),
                            filter=FILTER, searchList=[conf], compilerSettings=DIRECTIVES)
        return template.respond()

    @cherrypy.expose
    def addSchedule(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg

        minute = kwargs.get('minute')
        hour = kwargs.get('hour')
        dayofweek = kwargs.get('dayofweek')
        action = kwargs.get('action')
        arguments = kwargs.get('arguments')

        arguments = arguments.strip().lower()
        if arguments in ('on', 'enable'):
            arguments = '1'
        elif arguments in ('off','disable'):
            arguments = '0'

        if minute and hour  and dayofweek and action:
            if (action == 'speedlimit') and arguments.isdigit():
                pass
            elif action in _SCHED_ACTIONS:
                arguments = ''
            elif action.find(':') > 0:
                if arguments == '1':
                    arguments = action
                    action = 'enable_server'
                else:
                    arguments = action
                    action = 'disable_server'
            else:
                action = None

            if action:
                sched = cfg.schedules()
                sched.append('%s %s %s %s %s' %
                                 (minute, hour, dayofweek, action, arguments))
                cfg.schedules.set(sched)

        config.save_config()
        scheduler.restart(force=True)
        raise dcRaiser(self.__root, kwargs)

    @cherrypy.expose
    def delSchedule(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg

        schedules = cfg.schedules()
        line = kwargs.get('line')
        if line and line in schedules:
            schedules.remove(line)
            cfg.schedules.set(schedules)
        config.save_config()
        scheduler.restart(force=True)
        raise dcRaiser(self.__root, kwargs)

#------------------------------------------------------------------------------
class ConfigNewzbin(object):
    def __init__(self, web_dir, root, prim):
        self.__root = root
        self.__web_dir = web_dir
        self.__prim = prim
        self.__bookmarks = []

    @cherrypy.expose
    def index(self, **kwargs):
        if cfg.configlock():
            return Protected()

        conf, pnfo_list, bytespersec = build_header(self.__prim)

        conf['username_newzbin'] = cfg.newzbin_username()
        conf['password_newzbin'] = cfg.newzbin_password.get_stars()
        conf['newzbin_bookmarks'] = int(cfg.newzbin_bookmarks())
        conf['newzbin_unbookmark'] = int(cfg.newzbin_unbookmark())
        conf['bookmark_rate'] = cfg.bookmark_rate()

        conf['bookmarks_list'] = self.__bookmarks

        conf['matrix_username'] = cfg.matrix_username()
        conf['matrix_apikey'] = cfg.matrix_apikey()

        template = Template(file=os.path.join(self.__web_dir, 'config_newzbin.tmpl'),
                            filter=FILTER, searchList=[conf], compilerSettings=DIRECTIVES)
        return template.respond()

    @cherrypy.expose
    def saveNewzbin(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg

        cfg.newzbin_username.set(kwargs.get('username_newzbin'))
        cfg.newzbin_password.set(kwargs.get('password_newzbin'))
        cfg.newzbin_bookmarks.set(kwargs.get('newzbin_bookmarks'))
        cfg.newzbin_unbookmark.set(kwargs.get('newzbin_unbookmark'))
        cfg.bookmark_rate.set(kwargs.get('bookmark_rate'))

        cfg.matrix_username.set(kwargs.get('matrix_username'))
        cfg.matrix_apikey.set(kwargs.get('matrix_apikey'))

        config.save_config()
        raise dcRaiser(self.__root, kwargs)

    @cherrypy.expose
    def saveMatrix(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg

        cfg.matrix_username.set(kwargs.get('matrix_username'))
        cfg.matrix_apikey.set(kwargs.get('matrix_apikey'))

        config.save_config()
        raise dcRaiser(self.__root, kwargs)


    @cherrypy.expose
    def getBookmarks(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg
        Bookmarks.do.run()
        raise dcRaiser(self.__root, kwargs)

    @cherrypy.expose
    def showBookmarks(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg
        self.__bookmarks = Bookmarks.do.bookmarksList()
        raise dcRaiser(self.__root, kwargs)

    @cherrypy.expose
    def hideBookmarks(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg
        self.__bookmarks = []
        raise dcRaiser(self.__root, kwargs)

#------------------------------------------------------------------------------

class ConfigCats(object):
    def __init__(self, web_dir, root, prim):
        self.__root = root
        self.__web_dir = web_dir
        self.__prim = prim

    @cherrypy.expose
    def index(self, **kwargs):
        if cfg.configlock():
            return Protected()

        conf, pnfo_list, bytespersec = build_header(self.__prim)

        if cfg.newzbin_username() and cfg.newzbin_password():
            conf['newzbinDetails'] = True

        conf['script_list'] = list_scripts(default=True)

        categories = config.get_categories()
        conf['have_cats'] =  categories != {}
        conf['defdir'] = cfg.complete_dir.get_path()


        empty = { 'name':'', 'pp':'-1', 'script':'', 'dir':'', 'newzbin':'', 'priority':DEFAULT_PRIORITY }
        slotinfo = []
        slotinfo.append(empty)
        for cat in sorted(categories):
            slot = categories[cat].get_dict()
            slot['name'] = cat
            slotinfo.append(slot)
        conf['slotinfo'] = slotinfo

        template = Template(file=os.path.join(self.__web_dir, 'config_cat.tmpl'),
                            filter=FILTER, searchList=[conf], compilerSettings=DIRECTIVES)
        return template.respond()

    @cherrypy.expose
    def delete(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg
        kwargs['section'] = 'categories'
        kwargs['keyword'] = kwargs.get('name')
        del_from_section(kwargs)
        raise dcRaiser(self.__root, kwargs)

    @cherrypy.expose
    def save(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg

        newname = kwargs.get('newname', '').strip()
        name = kwargs.get('name')
        if newname:
            if name:
                config.delete('categories', name)
            name = newname.lower()
            if kwargs.get('dir'):
                kwargs['dir'] = platform_encode(kwargs['dir'])
            config.ConfigCat(name, kwargs)

        config.save_config()
        raise dcRaiser(self.__root, kwargs)

    @cherrypy.expose
    def init_newzbin(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg

        config.define_categories(force=True)
        config.save_config()
        raise dcRaiser(self.__root, kwargs)


SORT_LIST = ( \
    'enable_tv_sorting', 'tv_sort_string', 'tv_categories',
    'enable_movie_sorting', 'movie_sort_string', 'movie_sort_extra', 'movie_extra_folder',
    'enable_date_sorting', 'date_sort_string', 'movie_categories', 'date_categories'
    )

#------------------------------------------------------------------------------
class ConfigSorting(object):
    def __init__(self, web_dir, root, prim):
        self.__root = root
        self.__web_dir = web_dir
        self.__prim = prim

    @cherrypy.expose
    def index(self, **kwargs):
        if cfg.configlock():
            return Protected()

        conf, pnfo_list, bytespersec = build_header(self.__prim)
        conf['complete_dir'] = cfg.complete_dir.get_path()

        for kw in SORT_LIST:
            conf[kw] = config.get_config('misc', kw)()
        conf['cat_list'] = list_cats(True)
        #tvSortList = []

        template = Template(file=os.path.join(self.__web_dir, 'config_sorting.tmpl'),
                            filter=FILTER, searchList=[conf], compilerSettings=DIRECTIVES)
        return template.respond()

    @cherrypy.expose
    def saveSorting(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg

        try:
            kwargs['movie_categories'] = kwargs['movie_cat']
        except:
            pass
        try:
            kwargs['date_categories'] = kwargs['date_cat']
        except:
            pass
        try:
            kwargs['tv_categories'] = kwargs['tv_cat']
        except:
            pass

        for kw in SORT_LIST:
            item = config.get_config('misc', kw)
            value = platform_encode(kwargs.get(kw))
            msg = item.set(value)
            if msg:
                return badParameterResponse(msg)

        config.save_config()
        raise dcRaiser(self.__root, kwargs)


#------------------------------------------------------------------------------

class ConnectionInfo(object):
    def __init__(self, web_dir, root, prim):
        self.__root = root
        self.__web_dir = web_dir
        self.__prim = prim
        self.__lastmail = None

    @cherrypy.expose
    def index(self, **kwargs):
        header, pnfo_list, bytespersec = build_header(self.__prim)

        header['logfile'] = sabnzbd.LOGFILE
        header['weblogfile'] = sabnzbd.WEBLOGFILE
        header['loglevel'] = str(cfg.log_level())

        header['lastmail'] = self.__lastmail

        header['servers'] = []

        for server in downloader.servers()[:]:
            busy = []
            connected = 0

            for nw in server.idle_threads[:]:
                if nw.connected:
                    connected += 1

            for nw in server.busy_threads[:]:
                article = nw.article
                art_name = ""
                nzf_name = ""
                nzo_name = ""

                if article:
                    nzf = article.nzf
                    nzo = nzf.nzo

                    art_name = xml_name(article.article)
                    #filename field is not always present
                    try:
                        nzf_name = xml_name(nzf.filename)
                    except: #attribute error
                        nzf_name = xml_name(nzf.subject)
                    nzo_name = xml_name(nzo.final_name)

                busy.append((nw.thrdnum, art_name, nzf_name, nzo_name))

                if nw.connected:
                    connected += 1

            if server.warning and not (connected or server.errormsg):
                connected = unicoder(server.warning)

            if server.request and not server.info:
                connected = T('server-resolving')
            busy.sort()

            header['servers'].append((server.host, server.port, connected, busy, server.ssl,
                                      server.active, server.errormsg, server.fillserver, server.optional))

        wlist = []
        for w in sabnzbd.GUIHANDLER.content():
            w = w.replace('WARNING', Ta('warning')).replace('ERROR', Ta('error'))
            wlist.append(xml_name(w))
        header['warnings'] = wlist

        template = Template(file=os.path.join(self.__web_dir, 'connection_info.tmpl'),
                            filter=FILTER, searchList=[header], compilerSettings=DIRECTIVES)
        return template.respond()

    @cherrypy.expose
    def disconnect(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg
        downloader.disconnect()
        raise dcRaiser(self.__root, kwargs)

    @cherrypy.expose
    def testmail(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg
        logging.info("Sending testmail")
        pack = {}
        pack['download'] = ['action 1', 'action 2']
        pack['unpack'] = ['action 1', 'action 2']

        self.__lastmail = emailer.endjob('I had a d\xe8ja vu', 123, 'unknown', True,
                                      os.path.normpath(os.path.join(cfg.complete_dir.get_path(), '/unknown/I had a d\xe8ja vu')),
                                      str(123*MEBI), pack, 'my_script', 'Line 1\nLine 2\nLine 3\nd\xe8ja vu\n', 0)
        raise dcRaiser(self.__root, kwargs)

    @cherrypy.expose
    def showlog(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg
        try:
            sabnzbd.LOGHANDLER.flush()
        except:
            pass
        return cherrypy.lib.static.serve_file(sabnzbd.LOGFILE, "application/x-download", "attachment")

    @cherrypy.expose
    def showweb(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg
        if sabnzbd.WEBLOGFILE:
            return cherrypy.lib.static.serve_file(sabnzbd.WEBLOGFILE, "application/x-download", "attachment")
        else:
            return "Web logging is off!"

    @cherrypy.expose
    def clearwarnings(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg
        sabnzbd.GUIHANDLER.clear()
        raise dcRaiser(self.__root, kwargs)

    @cherrypy.expose
    def change_loglevel(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg
        cfg.log_level.set(kwargs.get('loglevel'))
        config.save_config()

        raise dcRaiser(self.__root, kwargs)

    @cherrypy.expose
    def unblock_server(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg
        downloader.unblock(kwargs.get('server'))
        # Short sleep so that UI shows new server status
        time.sleep(1.0)
        raise dcRaiser(self.__root, kwargs)


def Protected():
    return badParameterResponse("Configuration is locked")

def badParameterResponse(msg):
    """Return a html page with error message and a 'back' button
    """
    return '''
<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.0//EN">
<html>
<head>
           <title>SABnzbd+ %s - %s/title>
</head>
<body>
<h3>%s</h3>
%s
<br><br>
<FORM><INPUT TYPE="BUTTON" VALUE="%s" ONCLICK="history.go(-1)"></FORM>
</body>
</html>
''' % (sabnzbd.__version__, T('error'), T('badParm'), unicoder(msg), T('button-back'))

def ShowFile(name, path):
    """Return a html page listing a file and a 'back' button
    """
    try:
        f = open(path, "r")
        msg = TRANS(f.read())
        f.close()
    except:
        msg = "FILE NOT FOUND\n"

    return '''
<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.0//EN">
<html>
<head>
           <title>%s</title>
</head>
<body>
<FORM><INPUT TYPE="BUTTON" VALUE="%s" ONCLICK="history.go(-1)"></FORM>
<h3>%s</h3>
<code><pre>
%s
</pre></code><br/><br/>
</body>
</html>
''' % (name, T('button-back'), name, escape(msg))

def ShowString(name, string):
    """Return a html page listing a file and a 'back' button
    """
    try:
        msg = TRANS(string)
    except:
        msg = "Encoding Error\n"

    return '''
<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.0//EN">
<html>
<head>
           <title>%s</title>
</head>
<body>
           <FORM><INPUT TYPE="BUTTON" VALUE="%s" ONCLICK="history.go(-1)"></FORM>
           <h3>%s</h3>
           <code><pre>
           %s
           </pre></code><br/><br/>
</body>
</html>
''' % (xml_name(name), T('button-back'), xml_name(name), escape(unicoder(msg)))


def ShowOK(url):
    return '''
<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.0//EN">
<html>
<head>
           <title>%s</title>
</head>
<body>
           <FORM><INPUT TYPE="BUTTON" VALUE="%s" ONCLICK="history.go(-1)"></FORM>
           <br/><br/>
           %s
           <br/><br/>
</body>
</html>
''' % (escape(url), T('button-back'), T('msg-reAdded@1') % escape(url))


def _make_link(qfeed, job):
    # Return downlink for a job
    url = job.get('url', '')
    status = job.get('status', '')
    title = job.get('title', '')
    cat = job.get('cat')
    pp = job.get('pp')
    script = job.get('script')
    prio = job.get('prio')
    rule = job.get('rule', 0)

    name = urllib.quote_plus(url)
    if 'nzbindex.nl/' in url or 'nzbindex.com/' in url or 'nzbclub.com/' in url:
        nzbname = ''
    else:
        nzbname = '&nzbname=%s' % urllib.quote(sanitize_foldername(latin1(title)))
    if cat:
        cat = '&cat=' + escape(cat)
    else:
        cat = ''
    if pp is None:
        pp = ''
    else:
        pp = '&pp=' + escape(str(pp))
    if script:
        script = '&script=' + escape(script)
    else:
        script = ''
    if prio:
        prio = '&priority=' + str(prio)
    else:
        prio = ''

    star = '&nbsp;*' * int(status.endswith('*'))
    if rule < 0:
        rule = '&nbsp;%s!' % T('msg-duplicate')
    else:
        rule = '&nbsp;#%s' % str(rule)

    if url.isdigit():
        title = '<a href="https://www.newzbin.com/browse/post/%s/" target="_blank">%s</a>' % (url, title)
    else:
        title = xml_name(title)

    return '<a href="rss_download?session=%s&feed=%s&id=%s%s%s%s%s%s">%s</a>&nbsp;&nbsp;&nbsp;%s%s%s<br/>' % \
           (cfg.api_key() ,qfeed, name, cat, pp, script, prio, nzbname, T('link-download'), title, star, rule)


def ShowRssLog(feed, all):
    """Return a html page listing an RSS log and a 'back' button
    """
    jobs = sabnzbd.rss.show_result(feed)
    names = jobs.keys()
    # Sort in the order the jobs came from the feed
    names.sort(lambda x, y: jobs[x].get('order', 0) - jobs[y].get('order', 0))

    qfeed = escape(feed.replace('/','%2F').replace('?', '%3F'))

    doneStr = []
    for x in names:
        job = jobs[x]
        if job['status'][0] == 'D':
            doneStr.append('%s<br/>' % xml_name(job['title']))

    goodStr = []
    for x in names:
        job = jobs[x]
        if job['status'][0] == 'G':
            goodStr.append(_make_link(qfeed, job))

    badStr = []
    for x in names:
        job = jobs[x]
        if job['status'][0] == 'B':
            badStr.append(_make_link(qfeed, job))

    if all:
        return '''
<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.0//EN">
<html>
<head>
               <title>%s</title>
</head>
<body>
               <form>
               <input type="submit" onclick="this.form.action='.'; this.form.submit(); return false;" value="%s"/>
               </form>
               <h3>%s</h3>
               %s<br/><br/>
               <b>%s</b><br/>
               %s
               <br/>
               <b>%s</b><br/>
               %s
               <br/>
               <b>%s</b><br/>
               %s
               <br/>
</body>
</html>
''' % (escape(feed), T('button-back'), escape(feed), T('explain-rssStar'), T('rss-matched'), \
       ''.join(goodStr), T('rss-notMatched'), ''.join(badStr), T('rss-done'), ''.join(doneStr))
    else:
        return '''
<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.0//EN">
<html>
<head>
               <title>%s</title>
</head>
<body>
               <form>
               <input type="submit" onclick="this.form.action='.'; this.form.submit(); return false;" value="%s"/>
               </form>
               <h3>%s</h3>
               <b>%s</b><br/>
               %s
               <br/>
</body>
</html>
''' % (escape(feed), T('button-back'), escape(feed), T('rss-downloaded'), ''.join(doneStr))



#------------------------------------------------------------------------------
LIST_EMAIL = (
    'email_endjob', 'email_full',
    'email_server', 'email_to', 'email_from',
    'email_account', 'email_pwd', 'email_dir', 'email_rss'
    )

class ConfigEmail(object):
    def __init__(self, web_dir, root, prim):
        self.__root = root
        self.__web_dir = web_dir
        self.__prim = prim

    @cherrypy.expose
    def index(self, **kwargs):
        if cfg.configlock():
            return Protected()

        conf, pnfo_list, bytespersec = build_header(self.__prim)

        conf['my_home'] = sabnzbd.DIR_HOME
        conf['my_lcldata'] = sabnzbd.DIR_LCLDATA

        for kw in LIST_EMAIL:
            conf[kw] = config.get_config('misc', kw).get_string()

        template = Template(file=os.path.join(self.__web_dir, 'config_email.tmpl'),
                            filter=FILTER, searchList=[conf], compilerSettings=DIRECTIVES)
        return template.respond()

    @cherrypy.expose
    def saveEmail(self, **kwargs):
        msg = check_session(kwargs)
        if msg: return msg

        for kw in LIST_EMAIL:
            msg = config.get_config('misc', kw).set(platform_encode(kwargs.get(kw)))
            if msg:
                return badParameterResponse(T('error-badValue@2') % (kw, unicoder(msg)))

        config.save_config()
        raise dcRaiser(self.__root, kwargs)


def rss_history(url, limit=50, search=None):
    url = url.replace('rss','')

    youngest = None

    rss = RSS()
    rss.channel.title = "SABnzbd History"
    rss.channel.description = "Overview of completed downloads"
    rss.channel.link = "http://sourceforge.net/projects/sabnzbdplus/"
    rss.channel.language = "en"

    items, fetched_items, max_items = build_history(limit=limit, search=search)

    for history in items:
        item = Item()

        item.pubDate = std_time(history['completed'])
        item.title = history['name']

        if not youngest:
            youngest = history['completed']
        elif history['completed'] < youngest:
            youngest = history['completed']

        if history['report']:
            item.link = "https://www.newzbin.com/browse/post/%s/" % history['report']
        elif history['url_info']:
            item.link = history['url_info']
        else:
            item.link = url

        stageLine = []
        for stage in history['stage_log']:
            stageLine.append("<tr><dt>Stage %s</dt>" % stage['name'])
            actions = []
            for action in stage['actions']:
                actions.append("<dd>%s</dd>" % (action))
            actions.sort()
            actions.reverse()
            for act in actions:
                stageLine.append(act)
            stageLine.append("</tr>")
        item.description = ''.join(stageLine)
        rss.addItem(item)

    rss.channel.lastBuildDate = std_time(youngest)
    rss.channel.pubDate = std_time(time.time())

    return rss.write()


def rss_warnings():
    """ Return an RSS feed with last warnings/errors
    """
    rss = RSS()
    rss.channel.title = "SABnzbd Warnings"
    rss.channel.description = "Overview of warnings/errors"
    rss.channel.link = "http://sourceforge.net/projects/sabnzbdplus/"
    rss.channel.language = "en"

    for warn in sabnzbd.GUIHANDLER.content():
        item = Item()
        item.title = warn
        rss.addItem(item)

    rss.channel.lastBuildDate = std_time(time.time())
    rss.channel.pubDate = rss.channel.lastBuildDate
    return rss.write()