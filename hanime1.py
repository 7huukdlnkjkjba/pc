from __future__ import annotations

import atexit
import base64
import gc
import hashlib
import importlib.metadata
import inspect
import json
import os
import shutil
import socket
import tempfile
import time
from pathlib import Path

from cloakbrowser import launch_persistent_context


# ============================================================================
# 配置
# ============================================================================
HEADLESS = False
TARGET_URL = "https://hanime1.com/watch?v=408185"

NDM_HOST = "127.0.0.1"
NDM_PORT = 10007
NDM_EXTENSION_ID = "pbghcbaeehloijjcebiflemhcebmlnke"

# True  = 每次全新临时 profile，用完即焚（推荐）
# False = 用下面的 PERSISTENT_PROFILE 保留登录状态；扩展目录仍然临时、仍然删
USE_TEMP_WORKSPACE = True
PERSISTENT_PROFILE = Path(r"C:\Users\Administrator\Documents\ndm_profile_edge")

REPORT_FILE = Path("cloakbrowser_media_diagnostic.json")
MAX_NETWORK_EVENTS = 500


CHROMIUM_ARGS = [
    "--test-type",
    "--disable-infobars",
    "--no-default-browser-check",
    "--no-first-run",
    "--hide-crash-restore-bubble",
    "--disable-session-crashed-bubble",
    "--disable-features=Translate,TranslateUI,AcceptCHFrame,"
    "MediaRouter,OptimizationHints,ChromeWhatsNewUI",
    "--lang=zh-CN",
    "--disable-component-update",
]


# ============================================================================
# ★ 临时目录管理：创建时自动注册，退出时自动删除
# ============================================================================
_cleanup_dirs: list[Path] = []


def _atexit_cleanup() -> None:
    for p in list(_cleanup_dirs):
        try:
            robust_rmtree(p, retries=6, delay=0.3, verbose=False)
        except Exception:
            pass
    _cleanup_dirs.clear()


atexit.register(_atexit_cleanup)


def make_temp_dir(prefix: str = "ndm_diag_") -> Path:
    """创建一个临时目录，自动注册到退出清理列表。"""
    p = Path(tempfile.mkdtemp(prefix=prefix))
    _cleanup_dirs.append(p)
    return p


def robust_rmtree(path: Path, retries: int = 15, delay: float = 0.5,
                  verbose: bool = True) -> bool:
    """
    Windows 上删除临时目录的健壮版。
    Chromium 退出后可能仍持有文件锁，需要 retry + chmod + gc。
    """
    path = Path(path)
    if not path.exists():
        return True

    def _on_error(func, p, exc_info):
        try:
            os.chmod(p, 0o777)
            func(p)
        except Exception:
            pass

    for i in range(retries):
        try:
            shutil.rmtree(path, onerror=_on_error)
        except Exception:
            pass
        if not path.exists():
            if verbose and i > 0:
                print(f"[CLEANUP] 第 {i+1} 次重试成功")
            return True
        gc.collect()
        time.sleep(delay)

    # 最后一搏：忽略所有错误
    shutil.rmtree(path, ignore_errors=True)
    ok = not path.exists()
    if verbose and not ok:
        print(f"[CLEANUP][WARN] 无法完全删除：{path}")
    return ok


def cleanup_all(verbose: bool = True) -> None:
    """立即删除所有注册的临时目录。"""
    for p in list(_cleanup_dirs):
        ok = robust_rmtree(p, verbose=verbose)
        if verbose:
            print(f"[CLEANUP] {'OK ' if ok else 'FAIL'}  {p}")
    _cleanup_dirs.clear()


# ============================================================================
# 内嵌扩展（fallback，仅当本机找不到已安装 NDM 扩展时使用）
# ============================================================================
EMBEDDED_MANIFEST = "{\n  \"author\": \"Javad Motallebi\",\n  \"background\": {\n    \"service_worker\": \"bg.js\"\n  },\n  \"content_scripts\": [\n    {\n      \"all_frames\": true,\n      \"js\": [\n        \"ct.js\"\n      ],\n      \"matches\": [\n        \"http://*/*\",\n        \"https://*/*\"\n      ],\n      \"run_at\": \"document_start\"\n    }\n  ],\n  \"description\": \"Sends Download Links to Neat Download Manager\",\n  \"homepage_url\": \"https://www.neatdownloadmanager.com/\",\n  \"host_permissions\": [\n    \"<all_urls>\"\n  ],\n  \"key\": \"MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAz0KZ9wDknggIqdwLwOUoJZjEOS38gNqHFsVWJqdYzqbJpbL4TowiZolT2m9ErEvx5bNy+sFjnM0rjXoSlMHbPGp0ZJgfaIpV0xA8GZB45G3HDmwji/dpRaDBiluwPuGwwA8VMmu2kko+qN1O50lLFv7VVj6/AloRWMnTRS5a4Nsk1wkJQY16DBIS2qzg3onvmOQm7uSKfbRhQuU4Nkabt0QaprA3O87jQ0LHLL580V5TJNG00ka0mX46H7Sy/nn+3XQqS/hVkD0gujgLTGo/9CEjaJVstBHCYEiJ8GD/UzZEP852T0gU8sFTGjaRUD3OcDcAyN9qV8YG1oaZN+v6aQIDAQAB\",\n  \"manifest_version\": 3,\n  \"name\": \"NeatDownloadManager Extension\",\n  \"permissions\": [\n    \"webRequest\",\n    \"webNavigation\",\n    \"cookies\",\n    \"contextMenus\",\n    \"storage\",\n    \"downloads\"\n  ],\n  \"update_url\": \"https://edge.microsoft.com/extensionwebstorebase/v1/crx\",\n  \"version\": \"1.9.91\"\n}"
EMBEDDED_BG_JS = "var h=!1,aa=RegExp(\"^bytes [0-9]+-[0-9]+/([0-9]+)$\"),n=\"object xmlhttprequest media other main_frame sub_frame image\".split(\" \"),ba=[\"object\",\"xmlhttprequest\",\"media\",\"other\"],ca=RegExp(\"://.+/([^/]+?(?:.([^./]+?))?)(?=[?#]|$)\"),da=[301,302,303,307,308],ea=RegExp(\"^(?:application/x-apple-diskimage|application/download|application/force-download|application/x-msdownload|binary/octet-stream)$\",\"i\"),u=RegExp(\"^(?:FLV|SWF|MP3|MP4|M4V|F4F|F4V|M4A|MPG|MPEG|MPEG4|MPE|AVI|WMV|WMA|WAV|WAVE|ASF|RM|RAM|OGG|OGV|OGM|OGA|MOV|MID|MIDI|3GP|3GPP|QT|WEBM|TS|MKV|AAC|MP2T|MPEGTS|RMVB|VTT|SRT)$\",\n\"i\"),fa=RegExp(\"^(?:HTM|HTML|MHT|MHTML|SHTML|SHTM|XHT|XHTM|XHTML|XML|TXT|CSS|JS|JSON|GIF|ICO|JPEG|JPG|PNG|WEBP|BMP|SVG|TIF|TIFF|PDF|PHP|ASP|ASPX|EOT|TTF|WOF|WOFF|WOFF2|MSG|CHN|PEM|BR|OTF|ACZ|AZC|CGI|TPL|OSD|M3U8|DO)$\",\"i\"),ha=RegExp(\"^(?:FLV|AVI|MPG|MPE|WMV|QT|MOV|RM|RAM|WMA|MID|MIDI|AAC|MKV|RMVB)$\",\"i\"),C=RegExp(\"^(?:F4F|MPEGTS|TS|MP2T)$\",\"i\"),D={\"application/x-apple-diskimage\":\"DMG\",\"application/cert-chain+cbor\":\"MSG\",\"application/epub+zip\":\"EPUB\",\"application/java-archive\":\"JAR\",\"video/x-matroska\":\"MKV\",\n\"text/html\":\"HTML|HTM\",\"text/css\":\"CSS\",\"text/javascript\":\"JS|JSON\",\"text/mspg-legacyinfo\":\"MSI|MSP\",\"text/plain\":\"TXT|SRT\",\"text/srt\":\"SRT\",\"text/vtt\":\"VTT|SRT\",\"text/xml\":\"XML|F4M|TTML\",\"text/x-javascript\":\"JS|JSON\",\"text/x-json\":\"JSON\",\"application/f4m+xml\":\"F4M\",\"application/gzip\":\"GZ\",\"application/javascript\":\"JS\",\"application/json\":\"JSON\",\"application/msword\":\"DOC|DOCX|DOT|DOTX\",\"application/pdf\":\"PDF\",\"application/ttaf+xml\":\"DFXP\",\"application/vnd.apple.mpegurl\":\"M3U8\",\"application/zip\":\"ZIP\",\n\"application/x-7z-compressed\":\"7Z\",\"application/x-aim\":\"PLJ\",\"application/x-compress\":\"Z\",\"application/x-compress-7z\":\"7Z\",\"application/x-compressed\":\"ARJ\",\"application/x-gtar\":\"TAR\",\"application/x-msi\":\"MSI\",\"application/x-msp\":\"MSP\",\"application/x-gzip\":\"GZ\",\"application/x-gzip-compressed\":\"GZ\",\"application/x-javascript\":\"JS\",\"application/x-mpegurl\":\"M3U8\",\"application/x-msdos-program\":\"EXE|DLL\",\"application/vnd.apple.installer+xml\":\"MPKG\",\"application/x-ole-storage\":\"MSI|MSP\",\"application/x-rar\":\"RAR\",\n\"application/x-rar-compressed\":\"RAR\",\"application/x-sdlc\":\"EXE|SDLC\",\"application/x-shockwave-flash\":\"SWF\",\"application/x-silverlight-app\":\"XAP\",\"application/x-subrip\":\"SRT\",\"application/x-tar\":\"TAR\",\"application/x-zip\":\"ZIP\",\"application/x-zip-compressed\":\"ZIP\",\"video/3gpp\":\"3GP|3GPP\",\"video/3gpp2\":\"3GP|3GPP\",\"video/avi\":\"AVI\",\"video/f4f\":\"F4F\",\"video/f4m\":\"F4M\",\"video/flv\":\"FLV\",\"video/mp2t\":\"TS|M3U8\",\"video/mp4\":\"MP4|M4V\",\"video/mpeg\":\"MPG|MPEG|MPE\",\"video/mpegurl\":\"M3U8|M3U\",\"video/mpg4\":\"MP4|M4V\",\n\"video/msvideo\":\"AVI\",\"video/quicktime\":\"MOV|QT\",\"video/webm\":\"WEBM\",\"video/x-flash-video\":\"FLV\",\"video/x-flv\":\"FLV\",\"video/x-mp4\":\"MP4|M4V\",\"video/x-mpegurl\":\"M3U8|M3U\",\"video/x-mpg4\":\"MP4|M4V\",\"video/x-ms-asf\":\"ASF\",\"video/x-ms-wmv\":\"WMV\",\"video/x-msvideo\":\"AVI\",\"audio/3gpp\":\"3GP|3GPP\",\"audio/3gpp2\":\"3GP|3GPP\",\"audio/mp3\":\"MP3\",\"audio/mp4\":\"M4A|MP4\",\"audio/mp4a-latm\":\"M4A|MP4\",\"audio/mpeg\":\"MP3\",\"audio/mpeg4-generic\":\"M4A|MP4\",\"audio/mpegurl\":\"M3U8|M3U\",\"image/svg+xml\":\"SVG|SVGZ\",\"audio/webm\":\"WEBM\",\n\"audio/wav\":\"WAV\",\"audio/x-mpeg\":\"MP3\",\"audio/x-mpegurl\":\"M3U8|M3U\",\"audio/x-ms-wma\":\"WMA\",\"audio/x-wav\":\"WAV\",\"ilm/tm\":\"MP3\",\"image/gif\":\"GIF|GFA\",\"image/icon\":\"ICO|CUR\",\"image/jpg\":\"JPG|JPEG\",\"image/jpeg\":\"JPG|JPEG\",\"image/png\":\"PNG|APNG\",\"image/tiff\":\"TIF|TIFF\",\"image/vnd.microsoft.icon\":\"ICO|CUR\",\"image/webp\":\"WEBP\",\"image/x-icon\":\"ICO|CUR\",\"flv-application/octet-stream\":\"FLV\",\"image/x-xbitmap\":\"XBM\",\"audio/x-mp3\":\"MP3\",\"audio/x-hx-aac-adts\":\"AAC\",\"audio/aac\":\"AAC\",\"audio/x-aac\":\"AAC\",\"application/vnd.rn-realmedia-vbr\":\"RMVB\"};\nfunction E(a){return a&&unescape(a.split(\";\",1).shift().trim())||\"\"}function F(a){return(a=ca.exec(a))?a[1]||\"\":\"\"}function K(a){return-1<a.indexOf(\".\")?a.split(\".\").pop():\"\"}function ia(a){var b;a=a.toUpperCase();for(b in D)if(-1<D[b].split(\"|\").indexOf(a))return b;return\"\"}function L(a,b){if(!a)return null;for(var c=0;c<a.length;c++)if(a[c].name.toLowerCase()==b.toLowerCase())return a[c].value||a[c].binaryValue||null;return null}\nfunction M(){for(var a={},b=0;b<arguments.length;b++)for(var c in arguments[b])arguments[b].hasOwnProperty(c)&&(a[c]=arguments[b][c]);return a}function N(a,b){return a&&b&&0==a.indexOf(b)}function P(a,b){if(!a||!b)return!1;var c=a.length-b.length;return 0<=c&&a.indexOf(b,c)==c}function Q(a,b){return a&&b&&0<=a.indexOf(b)}function R(a){return Q(a,\"://\")?a.split(\"://\",1).shift().toLowerCase()||\"\":\"http\"}\nasync function S(a,b){var c=null,d={},e,f=b&&b[\"1\"]||\"GET\";if(b&&(e=b.m))for(var g=0;g<e.length;g++)N(e[g].name.toLowerCase(),\"x-\")&&(d[e[g].name]=e[g].value);if(\"POST\"==f&&b){try{T(b,b),b[\"10\"]&&(d[\"Content-Type\"]=b[\"10\"])}catch(m){}b&&b.postData&&(c=b.postData)}try{const m=await fetch(a[\"2\"],{method:f,credentials:\"include\",headers:new Headers(d),body:c});if(m.ok){let y=await m.text();(a.L||function(){})(y)}}catch(m){}}\nfunction U(){this[\"1\"]=\"GET\";this[\"2\"]=\"\";this[\"3\"]=\"\";this[\"4\"]=\"\";this[\"5\"]=\"\";this[\"6\"]=\"normal\";this[\"7\"]=0;this[\"8\"]=\"\";this[\"9\"]=\"\";this[\"10\"]=\"\";this.cookies=this[\"11\"]=\"\";this.postData=null}\nfunction V(){var a=this.constructor.prototype,b;for(b in a)this[b]=a[b].bind(this);this.H={};this.g={};this.j={};this.ga=1;this.s=\"\";this.C=!1;chrome.contextMenus.removeAll();chrome.contextMenus.create({title:\"Download by NeatDownloadManager\",id:\"NDM_CtxMenu\",contexts:[\"link\",\"image\"]});this.l(chrome.contextMenus.onClicked,this.X);this.l(chrome.downloads.onCreated,this.Y);this.l(chrome.runtime.onConnect,this.$);this.l(chrome.webRequest.onBeforeRequest,this.T,{urls:[\"http://*/*\",\"https://*/*\",\"ftp://*/*\"],\ntypes:n},[\"requestBody\"]);this.l(chrome.webRequest.onBeforeSendHeaders,this.U,{urls:[\"https://*/*\",\"http://*/*\"],types:n},[\"requestHeaders\"]);this.l(chrome.webRequest.onHeadersReceived,this.W,{urls:[\"<all_urls>\"],types:n},[\"responseHeaders\"]);this.l(chrome.webRequest.onCompleted,this.O,{urls:[\"<all_urls>\"]});this.l(chrome.webRequest.onErrorOccurred,this.O,{urls:[\"<all_urls>\"]});this.l(chrome.webNavigation.onHistoryStateUpdated,this.Z);chrome.action.onClicked.addListener(this.N);this.v=!1;chrome.action.setBadgeBackgroundColor({color:\"#FF3333\"});\nthis.N();var c=this;this.F=!0;chrome.storage.local.get([\"ShowMediaPanel\"],function(d){-1==d.ShowMediaPanel&&(c.F=!1)});this.i=this.G=null;this.D=!1;this.M()}var W=V.prototype;W.N=function(){var a=(this.v=!this.v)?\"\":\"Off\";chrome.action.setTitle({title:this.v?\"\":\"Download catcher is Off\\r\\nClick to toggle catching\"});chrome.action.setBadgeText({text:a})};W.Z=function(a){var b=this.g[[a.tabId,a.frameId]];b&&b[\"2\"]!=a.url&&(b.postMessage([11,a.url]),b[\"2\"]=a.url)};\nW.Y=function(a){h||!this.v?this.s=\"\":this.s!=a.finalUrl&&this.s!=a.url?this.s=\"\":(this.s=\"\",chrome.downloads.cancel(a.id),chrome.downloads.erase({id:a.id}))};\nW.I=async function(a){if(this.D){var b=\"1:\"+a[\"1\"]+\"\\r\\n\";b+=\"2:\"+a[\"2\"]+\"\\r\\n\";a[\"3\"]&&(b+=\"3:\"+a[\"3\"]+\"\\r\\n\");b+=\"6:\"+(a[\"6\"]||\"normal\")+\"\\r\\n\";a[\"4\"]&&(b+=\"4:\"+a[\"4\"]+\"\\r\\n\");if(a.pageUrl){var c=a.pageUrl,d=\"\";c&&=c.trim();c&&(d=(new URL(c)).origin);b+=\"Origin: \"+d+\"\\r\\n\"}if(a.pageUrl){if(c=a.pageUrl)d=c.lastIndexOf(\"#\"),c=0>d||d<c.indexOf(\"?\")?c:c.substr(0,d);b+=\"Referer: \"+c+\"\\r\\n\"}a[\"5\"]&&(b+=\"5:\"+a[\"5\"]+\"\\r\\n\");a.cookies&&(b+=\"Cookie: \"+a.cookies+\"\\r\\n\");a[\"10\"]&&(b+=\"Content-Type: \"+a[\"10\"]+\n\"\\r\\n\");a[\"11\"]&&(b+=\"Content-Disposition: \"+a[\"11\"]+\"\\r\\n\");a[\"9\"]&&(b+=\"9:\"+a[\"9\"]+\"\\r\\n\");for(var e in a)N(e.toLowerCase(),\"x-\")&&(b+=e+\": \"+a[e]+\"\\r\\n\");\"POST\"==a[\"1\"]&&(a[\"7\"]&&(b+=\"7:\"+a[\"7\"]+\"\\r\\n\"),a[\"8\"]&&(b+=\"8:\"+a[\"8\"]+\"\\r\\n\"),b=a.postData?b+(\"__0NeatPostData9__:\"+a.postData):b+\"Content-Length: 0\\r\\n\");if(!(118784<b.length))if(a[\"3\"])this.G.send(b),this.i=null;else if(\"POST\"==a[\"1\"]||!this.C||a[\"7\"]&&a[\"8\"])\"POST\"!=a[\"1\"]&&this.C&&(b+=\"8:\"+a[\"8\"]+\"\\r\\n\",b+=\"7:\"+a[\"7\"]+\"\\r\\n\"),this.G.send(b),\nthis.i=null;else try{const f=await fetch(a[\"2\"],{method:\"HEAD\",credentials:\"include\"});f.ok&&(a[\"8\"]=a[\"8\"]||f.headers.get(\"content-type\")||\"\",a[\"7\"]=a[\"7\"]||f.headers.get(\"Content-Length\")||0,b+=\"8:\"+a[\"8\"]+\"\\r\\n\",b+=\"7:\"+a[\"7\"]+\"\\r\\n\",this.G.send(b),this.i=null)}catch(f){}}else this.i=a,this.M()};W.M=function(){var a=new WebSocket(\"ws://127.0.0.1:10007/download\",\"neatextension.v1\");a.onopen=this.fa;a.onclose=this.ca;a.onmessage=this.ea;a.onerror=this.da;this.G=a};\nW.fa=function(){this.D=!0;this.i&&this.I(this.i)};W.ca=function(){this.D=!1;this.i=null};W.ea=function(a){a=a.data;\"waiting\"==a?this.C=!0:\"nowaiting\"==a?this.C=!1:!Q(a,\"Version\")&&N(a,\"ShowPanelEdge\")&&(a=\"1\"==a.split(\"=\")[1],a!=this.F&&(this.F=a,chrome.storage.local.set({la:a?1:-1},function(){}),this.ha([13,a])))};W.da=function(){this.D=!1;if(this.i){var a=this;chrome.tabs.query({currentWindow:!0,active:!0},function(b){b&&b.length&&(b=a.g[[b[0].id,0]])&&b.postMessage([15])})}this.i=null};\nW.J=function(a){if(this.i){var b=\"\";if(a&&0<a.length)for(var c=0;c<a.length;c++)b+=a[c].name+\"=\"+a[c].value+(c<a.length-1?\"; \":\"\");b=b.trim();this.i.cookies=b;this.I(this.i)}};W.X=function(a,b){var c=R(a.linkUrl);!c||\"ftp\"!=c&&\"http\"!=c&&\"https\"!=c||\"ftp\"==c&&!F(a.linkUrl)||(c=new U,c[\"2\"]=a.linkUrl||a.srcUrl,c.pageUrl=a.pageUrl,c[\"4\"]=b&&b.title||\"\",b&&b.url&&(c[\"5\"]=b.url),!c[\"5\"]&&(c[\"5\"]=a.pageUrl),this.i=c,chrome.cookies.getAll({url:c[\"2\"]},this.J))};function X(a){this.g=a}var ja=X.prototype;\nja.j=function(a){var b=\"\";if(!a)return b;if((a=a.split(\",\"))&&a.length)for(var c=0;c<a.length;c++){var d=a[c].split(\"=\");d&&2==d.length&&(\"BANDWIDTH\"==d[0].toString().trim()&&(b+=parseInt(parseInt(d[1])/1024)+\" Kbps \"),\"RESOLUTION\"==d[0].toString().trim()&&(b+=d[1]+\" \"))}return b.trim()};\nja.i=function(a,b){var c=[],d=0,e=\"\",f=this;b=b.split(/[\\r\\n]+/);if(0!=b.length&&\"#EXTM3U\"==b[0].trim()){for(var g=!1,m=!1,y=!1,p=\"\",t=RegExp(\"^#(EXT[^\\\\s:]+)(?::(.*))\"),G=1;G<b.length;G++){var k=b[G].trim();k&&(\"#\"==k[0]?0==k.indexOf(\"#EXT\")&&(k=t.exec(k))&&(g||(g=\"EXTINF\"==k[1])&&(p=k[2]),m||(m=\"EXT-X-STREAM-INF\"==k[1])&&(p=k[2]),y||=\"EXT-X-BYTERANGE\"==k[1]):(g&&(d+=parseFloat(p),g=!1),m&&(c.push({2:(new URL(k,a[\"2\"])).href,tags:p}),m=!1),y&&!e&&(e=(new URL(k,a[\"2\"])).href)))}if(e){b=\"\";d&&(60<\nd&&(b+=parseInt(d/60)+\" min \"),b+=parseInt(d%60)&&parseInt(d%60)+\" sec\");var l={6:\"media\",fEx:\"ts\",4:\"TS File \"+b,fDu:b};l=M(l,{1:a[\"1\"],2:e,tabId:a.tabId,frameId:a.frameId,fS:a[\"7\"],fileName:a.fileName});Y(a,l);\"POST\"==l[\"1\"]&&T(a,l);setTimeout(function(){f.g.A(l)},2500)}else c.length?setTimeout(function(){for(var B=0;B<c.length;B++)f.g.A(M({tabId:a.tabId,frameId:a.frameId},{1:\"GET\",2:c[B][\"2\"],6:\"hls\",fEx:\"ts\",4:\"TS File \"+f.j(c[B].tags)}))},2500):0<d&&(b=\"\",60<d&&(b+=parseInt(d/60)+\" min \"),b+=\nparseInt(d%60)&&parseInt(d%60)+\" sec\",l={6:\"hls\",fEx:\"ts\",4:\"TS File \"+b,fDu:b},l=M(l,{1:a[\"1\"],2:a[\"2\"],tabId:a.tabId,frameId:a.frameId,fS:a[\"7\"],fileName:a.fileName}),Y(a,l),\"POST\"==l[\"1\"]&&T(a,l),setTimeout(function(){f.g.A(l)},2500))}};W.A=function(a){var b=this.g[[a.tabId,a.frameId]];if(!b&&(b=this.g[[a.tabId,0]],!b))return;var c=a[\"2\"],d=0,e;var f=0;for(e=c.length;f<e;f++){var g=c.charCodeAt(f);d=(d<<5)-d+g;d|=0}a.id=d;b.postMessage([1,a,b[\"2\"]])};W.O=function(a){delete this.j[a.requestId]};\nfunction ka(a,b){if(!a)return null;var c=a.raw;if(c){a=\"\";for(b=0;b<c.length;b++){var d=c[b].bytes;if(!d)return null;d=new Uint8Array(d);for(var e=d.length,f=0;f<e;f++)a+=String.fromCharCode(d[f])}return a}c=a.formData;if(!c)return null;e=E(b);a=[];e&&=e.toLowerCase();if(\"application/x-www-form-urlencoded\"==e){for(d in c)for(e=c[d],d=d.split(\" \").map(encodeURIComponent).join(\"+\"),b=0;b<e.length;b++)a.length&&a.push(\"&\"),a.push(d,\"=\",e[b].split(\" \").map(encodeURIComponent).join(\"+\"));return a.join(\"\")}if(\"multipart/form-data\"==\ne){(f=Z(b,\"boundary\"))||(f=\"----WebKitFormBoundary\"+Math.random().toString(36).substr(2));for(d in c)for(e=c[d],b=0;b<e.length;b++)a.push(\"--\",f,'\\r\\nContent-Disposition: form-data; name=\"',d,'\"\\r\\n\\r\\n',e[b],\"\\r\\n\");a.push(\"--\",f,\"--\\r\\n\");return a.join(\"\")}return null}\nW.V=function(a){if(!(\"video/webm\"!=a[\"8\"].toLowerCase()&&\"audio/webm\"!=a[\"8\"].toLowerCase()||1>a[\"2\"].indexOf(\"signature=\")&&1>a[\"2\"].indexOf(\"sig=\"))){var b=this.g[[a.tabId,a.frameId]];b||=this.g[[a.tabId,0]];if(b){var c=a[\"2\"].indexOf(\"?\");if(-1!=c){var d=a[\"2\"].substring(0,c);c=a[\"2\"].substring(c+1);a={2:\"\",mme:a[\"8\"].split(\"/\").shift(),ig:0,du:0,mK:\"\",purl:b[\"2\"]};d+=\"?\";c=c.split(\"&\");for(var e=0;e<c.length;e++)N(c[e],\"dur=\")&&(a.du=parseFloat(c[e].split(\"=\").pop())),N(c[e],\"itag=\")&&(a.ig=c[e].split(\"=\").pop()),\nN(c[e],\"ei=\")&&(a.mK=c[e].split(\"=\").pop()),N(c[e],\"range=\")||N(c[e],\"rbuf=\")||N(c[e],\"rn=\")||(d=d+c[e]+\"&\");a.du&&a.ig&&(d=d.substring(0,d.length-1),a[\"2\"]=d,b.postMessage([9,a]))}}}};\nW.W=function(a){var b,c=a.requestId,d=this;if(b=this.j[c]){var e=a.url,f=a.type,g=0<=ba.indexOf(f),m=a.method.toUpperCase(),y=R(e);if(!y||\"http\"!=y&&\"https\"!=y||\"GET\"!=m&&\"POST\"!=m)delete this.j[c];else{b.B=a.responseHeaders;var p=L(b.B,\"Content-Type\"),t=E(p).toLowerCase();if(\"image\"==f&&t&&N(t.toLowerCase(),\"image/\"))delete this.j[c];else{var G=L(b.B,\"Content-Disposition\"),k=\"attachment\"==E(G).toLowerCase();a=parseInt(a.statusLine.split(\" \",2).pop())||0;b.ia=0<=da.indexOf(a);if(!b.ia)if(200!=a&&\n206!=a)delete this.j[c];else{a=L(b.B,\"Content-Length\");var l=L(b.B,\"Content-Range\"),B=null;l&&(l=aa.exec(l))&&(a=l[1]);a&&(B=parseInt(a));if(0===B)delete this.j[c];else if(b[\"2\"]=e,b[\"8\"]=p,b[\"7\"]=B,b.type=f,b.protocol=y,b[\"1\"]=m,b.S=P(f,\"_frame\"),f=new URL(e),e=f.hostname,f=f.pathname,(m=f.split(\"/\").pop().trim())&&(m=m.split(\"?\").shift().trim()),b.o=m||\"\",b.u=K(b.o),b.K=Z(G,\"filename\")||Z(p,\"name\"),b.R=b.K&&K(b.K)||\"\",p=t?D[t]:!1,b.P=(p?p.split(\"|\").shift():\"\").toLowerCase(),b.h=b.P||b.R||b.u||\n\"\",b.fileName=b.K||b.o||\"\",b.fileName&&(p=b.fileName.lastIndexOf(\".\"),-1<p&&(b.fileName=b.fileName.substr(0,p).trim())),b.fileName&&b.h&&(b.fileName+=\".\"+b.h),!t&&b.h&&(t=ia(b.h)),p=\"main_frame\"==b.type&&u.test(b.h)&&!C.test(b.h),Q(b.o.toLowerCase(),\"manif\")||Q(b.o.toLowerCase(),\"favicon.ico\")||Q(b.o.toLowerCase(),\"pem.msg\")||P(b.o.toLowerCase(),\".wasm\")||Q(b.o.toLowerCase(),\".json\")||Q(b.P.toLowerCase(),\"json\")||Q(b.R.toLowerCase(),\"json\")||!(p||\"other\"==b.type&&u.test(b.h)||(b.S||!g)&&ha.test(b.h)||\n(b.S||\"other\"==b.type)&&(k||ea.test(t)||b.h&&!u.test(b.h)&&!fa.test(b.h))))if(Q(e,\"youtube.com\")&&Q(f,\"api/timedtext\")){if(g=b[\"2\"].indexOf(\"?\"),-1!=g){c=b[\"2\"].substring(0,g)+\"?\";g=b[\"2\"].substring(g+1).split(\"&\");for(t=0;t<g.length;t++)c=N(g[t],\"fmt=\")?c+\"fmt=vtt&\":c+g[t]+\"&\";\"&\"==c[c.length-1]&&(c=c.substring(0,c.length-1));b[\"2\"]=c;var H={2:b[\"2\"],6:\"media\",1:b[\"1\"],tabId:b.tabId,frameId:b.frameId,fEx:\"VTT\",7:b[\"7\"],8:b[\"8\"],fS:b[\"7\"],fileName:b.fileName};setTimeout(function(){d.A(H)},1500)}}else{k=\n\"vtt\"==b.h.toLowerCase()||\"vtt\"==b.u.toLowerCase()||\"srt\"==b.h.toLowerCase()||\"srt\"==b.u.toLowerCase();var O=null;\"m3u8\"==b.h.toLowerCase()||\"m3u8\"==b.u.toLowerCase()?O=new X(this):k||\"POST\"==b[\"1\"]||Q(e.toLowerCase(),\"vimeo\")||Q(e.toLowerCase(),\"youtube\")||Q(e.toLowerCase(),\"google\")||\"txt\"!=b.h.toLowerCase()&&\"js\"!=b.h.toLowerCase()||\"xmlhttprequest\"!=b.type||b[\"7\"]&&307200<b[\"7\"]||(O=new X(this));if(O)S({2:b[\"2\"],L:function(v){O.i(M({},b),v)}},M({},b));else if(g&&P(e,\"googlevideo.com\")&&N(f,\"/videoplayback\"))this.V(b);\nelse if(g&&RegExp(\"^(?:[w-]+.)*?(?:youtube.com|googlevideo.com|youtube.googleapis.com|docs.google.com)$\",\"i\").test(e)){if(P(f,\"player\")&&\"POST\"==b[\"1\"]&&0!=b.frameId){T(b,b);var q=b.postData;var r=q.indexOf('\"videoId\"');if(0>r)return;q=q.substr(r+9);r=q.indexOf('\"');if(0>r)return;var I=q.indexOf('\"',r+1);if(I<r)return;S({2:\"https://www.youtube.com/watch?v=\"+q.substr(r+1,I-r-1),L:function(v){for(var w=['\"formats\"',\"adaptiveFormats\"],z=0;z<w.length;z++)if(q=v,r=q.indexOf(w[z]),!(0>r||-1<q.indexOf(\"signatureCipher\"))){q=\nq.substr(r);r=q.indexOf(\"[\");I=q.indexOf(\"]\");if(0>r||0>I||I<=r)break;q=q.substr(r+1,I-r-1);(x=d.g[[b.tabId,b.frameId]])||(x=d.g[[b.tabId,0]]);x&&x.postMessage([7,q,1==z])}}},null)}}else g&&\"player.vimeo.com\"==e&&N(f,\"/video/\")&&\"application/json\"==t?S({2:b[\"2\"],L:function(v){var w=null;try{w=JSON.parse(v)}catch(J){}if(w){var z=w.request.files.progressive;z&&setTimeout(function(){for(var J=0;J<z.length;J++)d.A({1:\"GET\",2:z[J].url,6:\"media\",tabId:b.tabId,frameId:b.frameId,fEx:\"mp4\",4:\"MP4 File \"+z[J].quality})},\n2500)}}},b):!g&&!k||!u.test(b.h)&&!u.test(b.u)||C.test(b.h)||!(!b[\"7\"]||204800<b[\"7\"]||k)||\"ASF\"==b.h&&1024E3>=b[\"7\"]||\"DCLK-AdSvr\"==L(b.B,\"Server\")||(H={2:b[\"2\"],6:\"media\",1:b[\"1\"],tabId:b.tabId,frameId:b.frameId,fEx:u.test(b.h)?b.h:b.u,7:b[\"7\"],8:b[\"8\"],fS:b[\"7\"],fileName:b.fileName},\"POST\"==H[\"1\"]&&T(b,H),Y(b,H),setTimeout(function(){d.A(H)},2E3));delete this.j[c]}else{if(h||!this.v)this.s=\"\";else{this.s=b[\"2\"];var x=d.g[[b.tabId,b.frameId]];g=d.g[[b.tabId,0]];var A=M(new U,{2:b[\"2\"],1:b[\"1\"],\n4:g&&g[\"4\"]||x&&x[\"4\"],5:g&&g[\"2\"]||x&&x[\"2\"],7:b[\"7\"],8:b[\"8\"],pageUrl:x&&x[\"2\"]||b[\"2\"]});chrome.tabs.query({active:!0,currentWindow:!0},function(v){if(v&&v.length&&(b[\"2\"]==v[0].pendingUrl||b[\"2\"]==v[0].url)&&!A[\"5\"]&&v[0].openerTabId){var w=d.g[[v[0].openerTabId,0]];A[\"5\"]=w&&w[\"2\"];A[\"4\"]=w&&w[\"4\"];u.test(b.h)&&(chrome.tabs.remove(v[0].id),A[\"6\"]=\"media\")}});\"POST\"==A[\"1\"]&&T(b,A);Y(b,A);d.i=A;chrome.cookies.getAll({url:A[\"2\"]},d.J)}delete this.j[c]}}}}}};\nfunction T(a,b){var c=L(a.m,\"Content-Type\"),d=L(a.m,\"Content-Disposition\");a=ka(a.ka,c);if(!a||1>a.length)a=null;b.postData=a;c&&(b[\"10\"]=c.trim());d&&(b[\"11\"]=d.trim())}function Y(a,b){if(a.m)for(var c=0;c<a.m.length;c++)N(a.m[c].name.toLowerCase(),\"x-\")&&(b[a.m[c].name]=a.m[c].value)}W.U=function(a){if(!(0>a.tabId||0>a.frameId)){var b=this.j[a.requestId];b&&(b.m=a.requestHeaders)}};\nW.T=function(a){if(!(0>a.tabId||0>a.frameId))if(\"ftp\"==R(a.url)){if(F(a.url)&&!h){var b=new U,c=this.g[[a.tabId,0]];c&&c[\"2\"]&&(b[\"5\"]=c[\"2\"],b.pageUrl=c[\"2\"]);c&&c[\"4\"]&&(b[\"4\"]=c[\"4\"]);b[\"2\"]=a.url;this.I(b)}}else b=a.requestId,c=this.j[b]||{id:b,2:a.url,tabId:a.tabId,frameId:a.frameId},\"POST\"==a.method.toUpperCase()&&(c.ka=a.requestBody),this.j[b]=c};\nfunction Z(a,b){if(!a)return null;b=b.toLowerCase();a=a.split(\";\");a.shift();for(var c=0;c<a.length;c++){var d=a[c],e=d.indexOf(\"=\");if(0<e){var f=d.substr(0,e).trim().toLowerCase(),g=\"*\"==f[f.length-1];g&&(f=f.substr(0,f.length-1).trimRight());if(f==b)return a=d.substr(e+1).trim(),c=a.length-1,'\"'==a[0]&&'\"'==a[c]&&(a=a.substring(1,c)),g&&(a=a.split(\"'\",3).pop()),unescape(a)}else if(0>e&&d.trim().toLowerCase()==b)return\"\"}return null}W.l=function(a){a.addListener.apply(a,Array.prototype.slice.call(arguments).slice(1))};\nW.$=function(a){var b=a.sender.tab;if(b&&0<=b.id){var c=a.sender.frameId,d=a.id||this.ga++,e=b.id;a.id=d;a[\"4\"]=b.title;a.tabId=e;a.frameId=c;a.ja=0==c;a[\"2\"]=a.sender.url||a.ja&&b.url||null;a.onMessage.addListener(this.ba.bind(this,a));a.onDisconnect.addListener(this.aa.bind(this,a));this.H[d]=a;this.g[[e,c]]=a;a.postMessage([3,a.id]);a.postMessage([13,this.F]);a.sender=null}};\nW.ba=function(a,b){switch(b[0]){case 2:var c=b[2],d=b[3];(a=this.H[b[1]])&&c&&(a[\"2\"]=c);a&&d&&(a[\"4\"]=d);break;case 4:h=b[1];break;case 6:c=b[1];a=(a=a.tabId)&&this.g[[a,0]];var e=new U;e[\"1\"]=c[\"1\"]||\"GET\";e[\"2\"]=c[\"2\"];c[\"3\"]&&(e[\"3\"]=c[\"3\"]);e.pageUrl=b[2];e[\"4\"]=b[3]||a&&a[\"4\"]||\"\";e[\"5\"]=a&&a[\"2\"]||e.pageUrl;e[\"9\"]=b[4];c[\"7\"]&&(e[\"7\"]=c[\"7\"]);c[\"8\"]&&(e[\"8\"]=c[\"8\"]);e[\"6\"]=c[\"6\"]||\"media\";!c.fEx||\"vtt\"!=c.fEx.toLowerCase()&&\"srt\"!=c.fEx.toLowerCase()||(e[\"6\"]=\"normal\");c.postData&&(e.postData=\nc.postData);c[\"10\"]&&(e[\"10\"]=c[\"10\"]);c[\"11\"]&&(e[\"11\"]=c[\"11\"]);for(d in c)N(d.toLowerCase(),\"x-\")&&(e[d]=c[d]);this.i=e;chrome.cookies.getAll({url:e[\"2\"]},this.J)}};W.aa=function(a){for(var b in this.g)this.g[b]==a&&delete this.g[b];delete this.H[a.id]};W.ma=function(a,b){var c=this.g;a=a.toString()+\",\";for(var d in c)N(d,a)&&c[d].postMessage(b)};W.ha=function(a){var b=this.g,c;for(c in b)b[c].postMessage(a)};new V;\n"
EMBEDDED_CT_JS = "var k=chrome.runtime.getURL(\"img/icon16.png\"),w=chrome.runtime.getURL(\"img/close16.png\"),x={\"-1\":\"none\",1:\"\"},y={242:\"240p\",243:\"360p\",244:\"480p\",246:\"480p\",247:\"720p\",248:\"1080p\",271:\"1440p\",272:\"2160p\",278:\"144p\",302:\"720p-60f\",303:\"1080p-60f\",308:\"1440p-60f\",313:\"2160p\",315:\"2160p-60f\",335:\"1080p-60f\",336:\"1440p-60f\",337:\"2160p-60f\"},z=[171,172,249,250,251];function C(d){return document.getElementById(d)}window.el=C;\nfunction D(){var d=document.location.host.toLowerCase(),g=d.length-12;return 0<=g&&d.indexOf(\"facebook.com\",g)==g}async function E(d){try{const g=await fetch(d[\"2\"],{mode:\"no-cors\"});if(g.ok){let a=await g.text();(d.ba||function(){})(a)}}catch(g){}}function F(d){var g=0,a;var b=0;for(a=d.length;b<a;b++){var c=d.charCodeAt(b);g=(g<<5)-g+c;g|=0}return g}\nfunction G(d){return!d||0>d?\" \":1E3>d?d+\" Bytes\":1E6>d?(d/1024).toFixed(1)+\" KB\":1E9>d?(d/1048576).toFixed(2)+\" MB\":(d/1073741824).toFixed(3)+\" GB\"}function H(d){return d.replace(/\\\\u([\\d\\w]{4})/gi,function(g,a){return String.fromCharCode(parseInt(a,16))})}function I(d){if(!d)return{left:0,top:0};try{var g=d.getBoundingClientRect();return g?{left:Math.round(g.left+window.pageXOffset),top:Math.round(g.top+window.pageYOffset)}:{left:0,top:0}}catch(a){return{left:0,top:0}}}\nfunction M(d){return d?!d.fEx||\"VTT\"!=d.fEx.toUpperCase()&&\"SRT\"!=d.fEx.toUpperCase()?d[\"4\"]||(d.fEx.toUpperCase()||\"MP4\")+\" File  \"+(G(d.fS)||d.fDu):d.fEx.toUpperCase()+\" Subtitles File \"+(d.fS?G(d.fS):\" \"):\"Media File\"}function N(d,g,a){this.D=d;d.i[a]=this;this.ua=a;this.J=\"neatTable\"+a;this.O=\"neatHCell\"+a;this.h=null;this.m=g;this.j=null;this.position={left:0,top:0};this.F=-1;this.items=[];this.S=this.R=0;this.u=this.C=!1}var O=N.prototype;\nO.G=function(d){var g=Array.prototype.slice.call(arguments);g[2]=g[2].bind(this);d.addEventListener.apply(d,g.slice(1))};O.v=function(){this.h&&(this.h.style.left=this.position.left+\"px\",this.h.style.top=this.position.top+\"px\",this.h.style.zIndex+=500)};O.Y=function(d){this.K(!0);this.D.oa(this.items[d])};O.K=function(d){if(!d||-1!=this.F){var g=C(this.J).rows;this.F=d?-1:-this.F;for(d=1;d<g.length;d++)g[d].style.display=x[this.F]}};\nO.I=function(d){var g=this,a=C(this.J),b=this.D.N(this.items[d]);var c=a.insertRow(-1);c.cssText=\"all:revert;padding:0px;margin:0px;width:100%;line-height:100% !important;height:19px !important\";c.style.display=x[g.F];a=c.insertCell(0);a.style.cssText=\"all:revert;letter-spacing:normal;line-height:100% !important;width:100%;height:19px !important;margin:0px;padding:0px;padding-left:5px;vertical-align:middle;color:black !important;cursor:default;border:dotted 1px black;background:#c9dff2 !important;direction:ltr;text-align:left;font-family:tahoma !important;font-style:normal;font-weight:bold;font-size:7pt !important\";\nb=M(b);if(0==d&&1==this.items.length){c=document.createElement(\"TABLE\");c.style.cssText=\"all:revert;border-spacing:0px;border-collapse:separate;padding:0px;margin:0px;width:100%;border:solid 1px black;direction:ltr;line-height:100% !important\";var f=c.insertRow(-1);f.style.cssText=\"all:revert;padding:0px;margin:0px;line-height:100% !important;height:19px !important\";var e=f.insertCell(-1);e.style.cssText=\"all:revert;background:#c9dff2 !important;padding:0px;margin:0px;width:20px;height:19px !important;text-align:center;vertical-align:middle;line-height:100% !important\";\nvar h=document.createElement(\"IMG\");h.src=k;h.onclick=function(){alert(\"NeatDownloadManager Video/Audio Panel.\")};e.appendChild(h);e=f.insertCell(-1);e.id=g.O;e.style.cssText=\"all:revert;letter-spacing:normal;padding:0px;margin:0px;vertical-align:middle;color:black !important;cursor:default;background:#c9dff2 !important;direction:ltr;text-align:center;font-family:tahoma !important;font-style:normal;font-weight:bold;font-size:7pt !important;height:19px !important;line-height:100% !important\";e=f.insertCell(-1);\ne.style.cssText=\"all:revert;background:#c9dff2 !important;padding:0px;margin:0px;width:20px;height:19px !important;text-align:center;vertical-align:middle;line-height:100% !important\";f=document.createElement(\"IMG\");f.src=w;f.onclick=function(){g.h.style.display=\"none\"};e.appendChild(f);a.appendChild(c);a.style.paddingLeft=\"0px\";a=C(g.O);a.innerText=\" \"+b;a.onmouseover=function(){this.style.color=\"red\"};a.onmouseout=function(){this.style.color=\"black\"};a.onclick=function(){0==g.u&&g.Y(0);g.u=!1}}else a.innerText=\n\" \"+(d+1).toString()+\"- \"+b,a.onmouseover=function(){this.style.background=\"white\";this.style.color=\"red\"},a.onmouseout=function(){this.style.background=\"#c9dff2\";this.style.color=\"black\"},c.onmousedown=function(){g.Y(d)}};\nO.L=function(d){var g=this,a=this.D.N(d),b=null,c=this.m?\"absolute\":\"fixed\";this.m&&!this.C&&(b=I(this.m));b&&(this.position={left:Math.max(0,b.left-1),top:Math.max(0,b.top-19-4)});this.h?this.v():(this.h=document.createElement(\"DIV\"),this.h.style.cssText=\"all:revert;padding:0px;margin:0px;position:\"+c+\";z-index:100000000;width:210px;left:\"+this.position.left+\"px;top:\"+this.position.top+\"px;direction:ltr;text-align:center;background:#c9dff2 !important;line-height:100% !important;\",this.h.id=\"neatDiv\"+\nthis.ua,this.h.style.display=this.D.H?\"\":\"none\",document.body.appendChild(this.h),b=document.createElement(\"TABLE\"),b.id=this.J,b.style.cssText=\"all:revert;border-spacing:0px;border-collapse:separate;padding:0px;margin:0px;line-height:100% !important;direction:ltr;width:100%;\",this.h.appendChild(b),this.G(g.h,\"mousemove\",g.wa),this.G(g.h,\"mousedown\",g.va),this.G(g.h,\"mouseup\",g.T),this.G(g.h,\"mouseout\",g.xa),this.G(g.h,\"mouseover\",g.ya),this.j=setTimeout(function(){g.j=null;g.h.style.opacity=.45},\n3E4));if(!(-1<this.items.indexOf(d)&&\"hls\"!=a[\"6\"])){a=M(a);if(this.items.length&&(\" MP4 File HQ\"==a||\" MP4 File LQ\"==a))for(b=0;b<this.items.length;b++)if(a==M(this.D.N(this.items[b]))){this.items[b]=d;this.h.style.display=this.D.H?\"\":\"none\";this.v();return}this.items.push(d);d=this.items.length-1;a=C(g.J).rows;var f;d&&(f=C(g.O));0==d?this.I(0):1==d?(this.I(0),this.I(1),f.onclick=function(e){e.stopPropagation();e.preventDefault();0==g.u&&g.K();g.u=!1},f.innerText=\" 2 Files\"):(this.I(d),f.innerText=\n\" \"+(d+1).toString()+\" Files\");a[0].style.display=\"\"}};O.va=function(d){0==d.button&&(this.C=!0,this.u=!1,this.R=d.clientX,this.S=d.clientY,d.stopPropagation(),d.preventDefault())};O.wa=function(d){if(this.C){this.K(!0);var g=d.clientX-this.R,a=d.clientY-this.S;!this.u&&(this.u=g||a);this.position.left+=g;this.position.top+=a;this.R=d.clientX;this.S=d.clientY;this.v()}else this.u=!1};\nO.xa=function(){this.C=!1;this.j&&(clearTimeout(this.j),this.j=null);var d=this;this.j=setTimeout(function(){d.h.style.opacity=.45;d.j=null},15E3)};O.ya=function(){this.C=!1;this.j&&(clearTimeout(this.j),this.j=null);this.h.style.opacity=1};O.T=function(){this.C=!1};\nif(!window.o){var P=function(){this.ea=null;this.A={};this.i={};this.g=[];this.P=!1;this.$=this.Z=-1;this.Counter=1;this.l=null;this.H=!0;this.ja=[];this.port=chrome.runtime.connect({name:\"neat\"});this.ga=Math.ceil(2E6*Math.random());this.port.onMessage.addListener(this.aa.bind(this));this.port.onDisconnect.addListener(this.ca.bind(this));if(D()){var a=this;this.sa=new window.MutationObserver(function(b){b.forEach(function(c){a.pa(c.target)})});this.sa.observe(document,{childList:!0,subtree:!0})}this.o(window,\n\"keydown\",this.W,!0);this.o(window,\"keyup\",this.W,!0);this.o(window,\"mouseup\",this.T,!0);this.o(window,\"resize\",this.ta);this.o(document,\"DOMContentLoaded\",this.da);this.o(document,\"click\",this.ra)};window.o=!0;O=P.prototype;O.qa=function(a,b){b.hd&&this.B({id:F(b.hd),1:\"GET\",2:b.hd,fEx:\"mp4\",4:\" MP4 File HQ\"},window.location.href,a,!1);b.sd&&this.B({id:F(b.sd),1:\"GET\",2:b.sd,fEx:\"mp4\",4:\" MP4 File LQ\"},window.location.href,a,!1)};O.la=function(a){for(;(a=a.parentElement)&&1>a.querySelectorAll(\"video\").length;);\nif(a)return a.querySelectorAll(\"video\")[0]};\nO.Ba=function(){this.port.postMessage([2,this.ea,window.location.href,this.getTitle()])};O.da=function(a){for(var b=this,c=document.getElementsByTagName(\"SCRIPT\"),f,e,h,l,m=!1,q=/\"progressive\":\\s*\\[/,r=0;r<c.length;r++){var n=c[r];f=n.innerText;if(a&&!m&&-1<f.indexOf(\"itag\")&&0>f.indexOf(\"signatureCipher\")){for(var p=\n['\"formats\"',\"adaptiveFormats\"],t=0;t<p.length;t++)e=f,h=e.indexOf(p[t]),0>h||(e=e.substr(h),h=e.indexOf(\"[\"),l=e.indexOf(\"]\"),0>h||0>l||l<=h||(e=e.substr(h+1,l-h-1),m=this.M(e,1==t)));if(this.Ca)break}if(0<=document.location.host.toLowerCase().indexOf(\"vimeo\")&&!n.src&&q.test(n.innerText)&&(e=n.innerText,h=e.indexOf('\"progressive\"'),!(0>h||(l=e.indexOf(\"]\",h),0>l)))){e=e.substr(h,l-h+1);f=null;try{f=JSON.parse(\"{\"+e+\"}\")}catch(v){}if(f){var u=f.progressive;u&&setTimeout(function(){for(var v=0;v<\nu.length;v++)b.B({id:F(u[v].url),1:\"GET\",2:u[v].url,fEx:\"mp4\",4:\"MP4 File \"+u[v].quality},window.location.href,null,!1)},2E3);break}}}};O.M=function(a,b){var c=this,f={18:{e:\"MP4\",s:\"360p\"},22:{e:\"MP4\",s:\"720p\"},37:{e:\"MP4\",s:\"1080p\"},38:{e:\"MP4\",s:\"1080p\"},82:{e:\"MP4\",s:\"360p\"},84:{e:\"MP4\",s:\"720p\"},132:{e:\"MP4\",s:\"240p\"},151:{e:\"MP4\",s:\"144p\"}};a=H(a);a=a.replace(/\\\\/g,\"\");var e=[],h=\"\";a=a.split(\"}\");if(1>a.length)return!1;for(var l=0;l<a.length;l++){var m=a[l].trim(),q={},r;if(m&&!(0>m.indexOf(\"itag\"))){var n=\nm.indexOf('\"url\"');if(!(0>n)){n=m.indexOf('\"',n+5);var p=m.indexOf('\"',n+1);if(!(0>n||0>p||p<=n||(m=q.url=decodeURIComponent(m.substr(n+1,p-n-1)),n=m.indexOf(\"?\"),0>n))){n=m.substring(n+1).split(\"&\");for(p=0;p<n.length;p++)0==n[p].indexOf(\"itag=\")&&(r=q.itag=parseInt(n[p].split(\"=\").pop())),0==n[p].indexOf(\"dur=\")&&(q.dur=parseFloat(n[p].split(\"=\").pop())),0==n[p].indexOf(\"ei=\")&&(q.mK=n[p].split(\"=\").pop());m&&r&&(0<m.indexOf(\"signature=\")||0<m.indexOf(\"sig=\"))&&(!b||q.dur)&&(b||f[r])&&(!b||y[r]||\n-1<z.indexOf(r))&&(b?e.push({2:m,mme:0>z.indexOf(r)?\"video\":\"audio\",ig:r,du:q.dur,mK:q.mK,purl:window.location.href}):(m=parseInt(q.dur),h=q.timeStr=60>m?m+\" sec\":parseInt(m/60)+\" min \"+(parseInt(m%60)?parseInt(m%60)+\" sec\":\"\"),e.push(q)))}}}}if(!e.length)return!1;b?setTimeout(function(){for(var t=e.length-1;0<=t;t--)c.X(e[t],\"DTC\")},1800):(this.Aa(),setTimeout(function(){for(var t=0;t<e.length;t++){var u=e[t];c.B({id:F(u.url),ig:u.itag,1:\"GET\",2:u.url,fEx:f[u.itag].e,4:f[u.itag].e+\" File \"+f[u.itag].s+\n\", \"+(u.timeStr||h)},window.location.href,null,!1)}},1500));return!0};O.Aa=function(){this.g=[];for(var a in this.i)for(var b=this.i[a],c=0;c<b.items.length;c++){var f=this.A[b.items[c]];if(f&&f.ig){this.U(a,!0);break}}};O.U=function(a,b){var c=this.i[a];if(c){if(b)for(b=0;b<c.items.length;b++)delete this.A[c.items[b]];try{document.body.removeChild(c.h),c.j&&clearTimeout(c.j)}catch(f){}delete this.i[a]}};O.oa=function(a){(a=this.A[a])&&this.port.postMessage([6,a,window.location.href,this.getTitle(),\nM(a)])};O.ka=function(a,b){var c=null,f=[\"VIDEO\",\"AUDIO\",\"OBJECT\",\"EMBED\"];try{var e=document.activeElement,h=0,l,m,q=e&&0<=f.indexOf(e.tagName)?e:null;q||=(e=document.elementFromPoint(this.Z,this.$))&&0<=f.indexOf(e.tagName)?e:null;for(var r=0;r<f.length;r++){for(var n=document.getElementsByTagName(f[r]),p=0;p<n.length;p++)if(e=n[p],3!=r||\"application/x-shockwave-flash\"==e.type.toLowerCase()){var t=e.src||e.data;if(t&&(t==a||t==b)){var u=e;break}if(q||v)var v=e;else{var A=e.clientWidth,B=e.clientHeight;\nif(A&&B){var J=window.getComputedStyle(e);if(!J||\"hidden\"!=J.visibility){var K=A*B;B<1.4*A&&A<3*B&&K>h&&(h=K,l=e);m||=e}}}}if(u)break}(c=u||q||v||l||m)||(c=document.querySelectorAll(\"video,audio\")[0]);if(!c)return null;if(\"EMBED\"==c.tagName&&!c.clientWidth&&!c.clientHeight){var L=c.parentElement;\"OBJECT\"==L.tagName&&(c=L)}return c}catch(Q){return null}};O.ma=function(a){try{var b=parseInt(a.getAttribute(\"JM_NEAT\"));b||(b=this.ga<<10|this.Counter++,a.setAttribute(\"JM_NEAT\",b));return b}catch(c){}};\nO.getTitle=function(){var a=\"\";try{a=document.title||document.getElementsByTagName(\"title\")[0].innerText,a=a.trim()}catch(b){}return a?a=a.replace(/[ \\t\\r\\n\\u25B6]+/g,\" \").trim():\"\"};O.W=function(a){8!=a.keyCode&&46!=a.keyCode||this.port.postMessage([4,\"keydown\"==a.type])};O.T=function(a){0==a.button&&(this.Z=a.clientX,this.$=a.clientY)};O.ta=function(){if(!this.P){this.P=!0;var a=this;window.setTimeout(function(){for(var b in a.i){var c=a.i[b],f=null;c.m&&(f=I(c.m));if(f){try{document.body.removeChild(c.h)}catch(e){}c.position.left=\nMath.max(0,f.left-1);c.position.top=Math.max(0,f.top-19-4);document.body.appendChild(c.h)}c.v()}a.P=!1},500)}};function d(a,b){return 18>Math.abs(a.left-b.left)&&18>Math.abs(a.top-b.top)}function g(a){a=I(a.m);return!a||0>a.left||0>a.top}O.B=function(a,b,c,f){var e=this,h=-1,l=null,m;f=f&&RegExp(\".*facebook.com$|.*vimeo.com$|.*youtube.com$\",\"i\").test(window.location.host)&&!(a.fEx&&\"VTT\"==a.fEx.toUpperCase())&&!(!a.fS||4194304<a.fS);a.id||(a.id=F(a[\"2\"]));c||=this.ka(a[\"2\"],b);if(!c)for(m in this.i){l=\nthis.i[m];h=m;break}if(!c&&!l){if(f)return;l=new N(e,null,0)}else if(!l)if(h=this.ma(c),l=this.i[h],!l){if(f)return;l=new N(e,c,h);b=I(c);c={left:Math.max(0,b.left-1),top:Math.max(0,b.top-19-4)};for(m in this.i)if(m&&m!=h&&(b=this.i[m],d(c,b.position))){for(c=0;c<b.items.length;c++)l.L(b.items[c]);this.U(m,!1);break}if(0!=h&&this.i[0]){b=this.i[0];for(c=0;c<b.items.length;c++)l.L(b.items[c]);this.U(0,!1)}}else{if(f){l.v();return}}else if(f){l.v();return}e.A[a.id]=a;l.L(a.id);l.m&&g(l)&&!this.l&&(e.l=\nsetInterval(function(){e.ha(l)},1200))};O.ha=function(a){if(a&&a.m){var b=I(a.m);b&&(a.position={left:Math.max(0,b.left-1),top:Math.max(0,b.top-19-4)},a.v());!b||0>b.left||0>b.top||(clearInterval(this.l),this.l=null)}else clearInterval(this.l),this.l=null};O.V=function(a,b){var c=this,f=a.du,e=\"\";e=60>f?parseInt(f)+\" sec\":parseInt(f/60)+\" min \"+(parseInt(f%60)?parseInt(f%60)+\" sec\":\"\");var h={id:F(a[\"2\"]+b[\"2\"]),2:a[\"2\"],3:b[\"2\"],ig:a.ig,4:\"MKV File \"+y[a.ig]+\", \"+e};setTimeout(function(){c.B(h,a.purl,\nnull,!1)},2200)};O.X=function(a,b){if(\"https://www.youtube.com/\"!=window.location.href.toLowerCase()&&(\"video\"!=a.mme||y[a.ig])){a.mode=b;for(b=0;b<this.g.length;b++)if(a.ig==this.g[b].ig&&(a.mK==this.g[b].mK||2>Math.abs(a.du-this.g[b].du)))return;a.used=!1;if(\"video\"==a.mme){var c=null;for(b=0;b<this.g.length;b++)if(\"audio\"==this.g[b].mme&&!this.g[b].used&&(a.mK==this.g[b].mK||2>Math.abs(a.du-this.g[b].du))){this.g[b].used=!0;c=this.g[b];break}if(!c)for(b=0;b<this.g.length;b++)if(\"audio\"==this.g[b].mme&&\n(a.mK==this.g[b].mK||2>Math.abs(a.du-this.g[b].du))){this.g[b].used=!0;c=this.g[b];break}c&&(a.used=!0,this.V(a,c))}else{c=null;for(b=0;b<this.g.length;b++)if(\"video\"==this.g[b].mme&&0==this.g[b].used&&(a.mK==this.g[b].mK||2>Math.abs(a.du-this.g[b].du))){this.g[b].used=!0;c=this.g[b];break}c&&(a.used=!0,this.V(c,a))}this.g.push(a)}};O.na=function(a){var b=this;E({2:a,ba:function(c){for(var f=['\"formats\"',\"adaptiveFormats\"],e=0;e<f.length;e++){var h=c,l=h.indexOf(f[e]);if(!(0>l||-1<h.indexOf(\"signatureCipher\"))){h=\nh.substr(l);l=h.indexOf(\"[\");var m=h.indexOf(\"]\");if(0>l||0>m||m<=l)break;h=h.substr(l+1,m-l-1);b.M(h,1==e)}}}})};O.aa=function(a){var b=this;switch(a[0]){case 1:b.B(a[1],a[2],null,!0);break;case 3:var c=a[1];c&&(b.ea=c);b.Ba();break;case 5:b.fa();break;case 7:b.M(a[1],a[2]);break;case 9:setTimeout(function(){b.X(a[1],\"BGH\")},1400);break;case 11:b.za();c=new URL(window.location.href);var f=c.pathname;if(!(0<=c.hostname.toLowerCase().indexOf(\"youtube.\"))){setTimeout(function(){b.da()},2500);break}0==\nf.toLowerCase().indexOf(\"/watch\")&&b.na(a[1]);break;case 13:c=a[1];c!=b.H&&(b.H=c,b.fa());break;case 15:alert(\"Extension Can't Connect to NeatDownloadManager Application, You Can : \\r\\n1- Check If NeatDownloadManager is Running.\\r\\n2- or Hold down Delete-Key and click on the Download link.\\r\\n3- or Disable NeatDownloadManager Extension temporarily.\")}};O.o=function(a){var b=Array.prototype.slice.call(arguments);b[2]=b[2].bind(this);this.ja.push(b);a.addEventListener.apply(a,b.slice(1))};O.ra=function(){for(var a in this.i)this.i[a].K(!0)};\nO.za=function(){try{for(var a in this.i)this.i[a].j&&clearTimeout(this.i[a].j),document.body.removeChild(this.i[a].h)}catch(b){}this.i={};this.A={};this.g=[];this.l&&clearInterval(this.l);this.l=null};O.fa=function(){var a=this.H?\"\":\"none\";try{for(var b in this.i)this.i[b].h.style.display=a}catch(c){}};O.ca=function(){this.port=chrome.runtime.connect({name:\"neat\"});this.port.onMessage.addListener(this.aa.bind(this));this.port.onDisconnect.addListener(this.ca.bind(this))};new P};\n"
EMBEDDED_PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR4nGNgAAIAAAUAAXpeqz8AAAAASUVORK5CYII="


# ============================================================================
# SW 诊断探针
# ============================================================================
SW_DIAG_INJECTION = r"""/* __NDM_DIAG_INJECTED__ */
(function () {
  const D = {
    loadedAt: Date.now(),
    wr_beforeRequest_count: 0,
    wr_headersReceived_count: 0,
    wr_lastBeforeRequest: null,
    wr_mp4_headers: [],
    ws_created: 0, ws_opened: 0, ws_closed: 0, ws_errored: 0,
    ws_lastUrl: null, ws_lastProtocol: null, ws_lastError: null,
    ws_lastCloseCode: null, ws_lastCloseReason: null,
    ws_sent_count: 0, ws_sent_last: null,
    ws_recv_count: 0, ws_recv_last: null,
    ndm_task_sent_count: 0, ndm_task_last: null,
    action_badgeText: null, action_title: null,
  };
  globalThis.__NDM_DIAG__ = D;

  function wrapWR(evt, name) {
    if (!evt || typeof evt.addListener !== "function") return;
    const orig = evt.addListener.bind(evt);
    evt.addListener = function (cb, filter, extra) {
      const wrapped = function (details) {
        try {
          if (name === "onBeforeRequest") {
            D.wr_beforeRequest_count++;
            D.wr_lastBeforeRequest = { url: details.url, type: details.type, method: details.method, tabId: details.tabId };
          } else if (name === "onHeadersReceived") {
            D.wr_headersReceived_count++;
            let ct = "";
            try {
              for (const h of details.responseHeaders || []) {
                if (h.name.toLowerCase() === "content-type") { ct = h.value || ""; break; }
              }
            } catch (_) {}
            if (/\.mp4|video\/|audio\//i.test(details.url + " " + ct)) {
              D.wr_mp4_headers.push({ t: Date.now(), url: details.url, type: details.type, statusLine: details.statusLine, content_type: ct });
              if (D.wr_mp4_headers.length > 30) D.wr_mp4_headers.shift();
            }
          }
        } catch (e) { D["wr_err_" + name] = String(e); }
        return cb(details);
      };
      return orig(wrapped, filter, extra);
    };
  }
  try {
    wrapWR(chrome.webRequest.onBeforeRequest, "onBeforeRequest");
    wrapWR(chrome.webRequest.onHeadersReceived, "onHeadersReceived");
  } catch (e) { D.wr_wrap_error = String(e); }

  const OrigWS = globalThis.WebSocket;
  if (OrigWS) {
    function Wrapped(url, protocols) {
      D.ws_created++;
      D.ws_lastUrl = String(url);
      D.ws_lastProtocol = Array.isArray(protocols) ? protocols.join(",") : String(protocols || "");
      const ws = new OrigWS(url, protocols);
      try {
        ws.addEventListener("open", () => { D.ws_opened++; D.ws_lastOpenAt = Date.now(); });
        ws.addEventListener("close", (e) => {
          D.ws_closed++; D.ws_lastCloseCode = e.code; D.ws_lastCloseReason = e.reason || "";
          D.ws_lastWasClean = e.wasClean; D.ws_lastCloseAt = Date.now();
        });
        ws.addEventListener("error", (e) => {
          D.ws_errored++;
          try { D.ws_lastError = e && e.message ? e.message : String(e); } catch (_) { D.ws_lastError = "unstringifiable"; }
          D.ws_lastErrorAt = Date.now();
        });
        ws.addEventListener("message", (e) => {
          D.ws_recv_count++;
          try { D.ws_recv_last = String(e.data).slice(0, 300); } catch (_) {}
        });
        const origSend = ws.send.bind(ws);
        ws.send = function (data) {
          D.ws_sent_count++;
          try {
            const s = String(data);
            D.ws_sent_last = s.slice(0, 500);
            if (/^1:/m.test(s) && /^2:/m.test(s)) {
              D.ndm_task_sent_count++;
              D.ndm_task_last = s.slice(0, 800);
            }
          } catch (_) {}
          return origSend(data);
        };
      } catch (_) {}
      return ws;
    }
    Wrapped.prototype = OrigWS.prototype;
    Wrapped.CONNECTING = OrigWS.CONNECTING;
    Wrapped.OPEN = OrigWS.OPEN;
    Wrapped.CLOSING = OrigWS.CLOSING;
    Wrapped.CLOSED = OrigWS.CLOSED;
    globalThis.WebSocket = Wrapped;
  }

  try {
    const origSetBadge = chrome.action.setBadgeText.bind(chrome.action);
    chrome.action.setBadgeText = function (details) {
      try { D.action_badgeText = details && details.text != null ? String(details.text) : null; } catch (_) {}
      return origSetBadge(details);
    };
    const origSetTitle = chrome.action.setTitle.bind(chrome.action);
    chrome.action.setTitle = function (details) {
      try { D.action_title = details && details.title ? String(details.title) : null; } catch (_) {}
      return origSetTitle(details);
    };
  } catch (_) {}

  try {
    setInterval(() => { try { chrome.runtime.getPlatformInfo(() => {}); } catch (_) {} }, 20000);
  } catch (_) {}
})();
"""


# ============================================================================
# 扩展准备：优先用本机安装版，其次用内嵌 fallback
# ============================================================================
def find_installed_ndm_extension() -> Path | None:
    local = os.environ.get("LOCALAPPDATA", "")
    if not local:
        return None

    bases = []
    for browser in ("Microsoft/Edge", "Google/Chrome"):
        for profile in ("Default", "Profile 1", "Profile 2", "Profile 3"):
            bases.append(Path(local) / browser / "User Data" / profile / "Extensions" / NDM_EXTENSION_ID)

    found: list[Path] = []
    for base in bases:
        if not base.exists():
            continue
        for version_dir in base.iterdir():
            if version_dir.is_dir() and (version_dir / "manifest.json").exists():
                found.append(version_dir)

    if not found:
        return None

    def vkey(p: Path):
        try:
            return tuple(int(x) for x in p.name.split("."))
        except ValueError:
            return (0,)

    found.sort(key=vkey, reverse=True)
    return found[0]


def prepare_extension(target: Path) -> str:
    """
    把扩展准备到 target 目录：
      - 优先从本机已安装的 NDM 扩展复制
      - 找不到才写内嵌 fallback
      - 最后注入 SW 诊断探针
    返回来源标记（"installed" 或 "embedded"）。
    """
    installed = find_installed_ndm_extension()
    if installed:
        print(f"[EXT] 找到本机 NDM 扩展：{installed}")
        target.mkdir(parents=True, exist_ok=True)
        shutil.copytree(installed, target, dirs_exist_ok=True)
        source = "installed"
    else:
        print("[EXT] 未找到已安装扩展，使用内嵌 fallback")
        img = target / "img"
        img.mkdir(parents=True, exist_ok=True)
        (target / "manifest.json").write_text(EMBEDDED_MANIFEST, encoding="utf-8")
        (target / "bg.js").write_text(EMBEDDED_BG_JS, encoding="utf-8")
        (target / "ct.js").write_text(EMBEDDED_CT_JS, encoding="utf-8")
        png = base64.b64decode(EMBEDDED_PNG_B64)
        for name in ("icon16.png","icon16_2x.png","icon48.png","icon128.png","close16.png","close16_2x.png"):
            (img / name).write_bytes(png)
        source = "embedded"

    # 注入 SW 探针
    bg = target / "bg.js"
    if bg.exists():
        original = bg.read_text(encoding="utf-8", errors="replace")
        if "__NDM_DIAG_INJECTED__" not in original:
            bg.write_text(SW_DIAG_INJECTION + "\n" + original, encoding="utf-8")

    return source


# ============================================================================
# 网络 / SW 诊断工具
# ============================================================================
def tcp_listening(host: str, port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1.5)
    try:
        s.connect((host, port)); return True
    except OSError:
        return False
    finally:
        s.close()


def read_sw_diag(context, ext_id: str):
    for worker in context.service_workers:
        if ext_id not in worker.url:
            continue
        try:
            return worker.evaluate("() => globalThis.__NDM_DIAG__ || null")
        except Exception as exc:
            return {"read_error": f"{type(exc).__name__}: {exc}"}
    return None


def wake_and_read(context, ext_id: str, retries: int = 8):
    last = None
    for _ in range(retries):
        s = read_sw_diag(context, ext_id)
        if s: return s
        last = s
        time.sleep(0.5)
    return last


def classify_sw(diag: dict) -> list[str]:
    if not diag:
        return ["SW_DIAG_UNAVAILABLE: 完全读不到扩展 SW 的诊断对象。"]
    out = []
    out.append("── webRequest ──")
    out.append(f"  onBeforeRequest 触发次数      : {diag.get('wr_beforeRequest_count', 0)}")
    out.append(f"  onHeadersReceived 触发次数    : {diag.get('wr_headersReceived_count', 0)}")
    mp4 = diag.get("wr_mp4_headers") or []
    out.append(f"  命中 mp4/video 的响应头       : {len(mp4)}")
    for m in mp4[-5:]:
        out.append(f"      {m.get('statusLine')}  {m.get('content_type')}  {m.get('url','')[:110]}")

    out.append("")
    out.append("── WebSocket ──")
    out.append(f"  创建/打开/关闭/错误           : {diag.get('ws_created')}/{diag.get('ws_opened')}/{diag.get('ws_closed')}/{diag.get('ws_errored')}")
    out.append(f"  URL                          : {diag.get('ws_lastUrl')}")
    out.append(f"  协议                         : {diag.get('ws_lastProtocol')}")
    out.append(f"  最后错误                     : {diag.get('ws_lastError')}")
    out.append(f"  最后关闭码/原因               : {diag.get('ws_lastCloseCode')} / {diag.get('ws_lastCloseReason')}")
    out.append(f"  发送/接收次数                 : {diag.get('ws_sent_count')} / {diag.get('ws_recv_count')}")
    out.append(f"  ★ 发往 NDM 的任务条数         : {diag.get('ndm_task_sent_count')}")
    if diag.get("ndm_task_last"):
        out.append(f"  最近一条任务内容              : {diag['ndm_task_last']}")

    out.append("")
    out.append("── 扩展状态 ──")
    out.append(f"  action badge 文本             : {diag.get('action_badgeText')!r}")
    out.append(f"  action title                  : {diag.get('action_title')!r}")

    out.append("")
    out.append("── 结论 ──")
    if diag.get("ws_created", 0) == 0:
        out.append("  扩展 SW 从未建立 WS → NDM 不可能收到任务。")
    elif diag.get("ws_opened", 0) == 0:
        out.append("  WS 连接全部失败 → 检查 NDM 是否在跑、端口是否为 10007。")
    elif diag.get("ndm_task_sent_count", 0) == 0:
        out.append("  WS 连上了，但扩展从未往 NDM 发任务。")
        out.append("  → 大概率是扩展的 URL 过滤逻辑把 mp4 排除了。")
    else:
        out.append(f"  扩展确实向 NDM 发送了 {diag['ndm_task_sent_count']} 条任务 → 问题在 NDM 桌面端。")
    return out


MEDIA_PROBE_JS = "\n() => {\n  const media = [...document.querySelectorAll(\"video,audio\")].map((v, i) => ({\n    index: i, tag: v.tagName, src: v.currentSrc || v.src || \"\",\n    readyState: v.readyState, networkState: v.networkState,\n    currentTime: Number.isFinite(v.currentTime) ? v.currentTime : null,\n    duration: Number.isFinite(v.duration) ? v.duration : null,\n    videoWidth: v.videoWidth, videoHeight: v.videoHeight,\n    error: v.error ? {code: v.error.code, message: v.error.message || \"\"} : null,\n  }));\n  return { url: location.href, title: document.title, media };\n}\n"


# ============================================================================
# main
# ============================================================================
def main() -> int:
    # --- 1) 准备所有临时目录 ---
    if USE_TEMP_WORKSPACE:
        profile_dir = make_temp_dir("ndm_diag_profile_")
        ext_dir = make_temp_dir("ndm_diag_ext_")
        print(f"[WORKSPACE] 临时 profile : {profile_dir}")
        print(f"[WORKSPACE] 临时扩展目录 : {ext_dir}")
    else:
        profile_dir = PERSISTENT_PROFILE
        profile_dir.mkdir(parents=True, exist_ok=True)
        ext_dir = make_temp_dir("ndm_diag_ext_")  # 扩展始终临时（要注入探针）
        print(f"[WORKSPACE] 持久 profile : {profile_dir}")
        print(f"[WORKSPACE] 临时扩展目录 : {ext_dir}")

    source = prepare_extension(ext_dir)
    ext_id = NDM_EXTENSION_ID

    context = None
    network_events, console_errors, page_errors = [], [], []
    report = {"extension_source": source, "extension_dir": str(ext_dir)}

    try:
        print("=" * 78)
        print("NDM 完整扩展 + 媒体诊断（用完即焚版）")
        print("=" * 78)
        try: cb_version = importlib.metadata.version("cloakbrowser")
        except Exception: cb_version = "unknown"

        print(f"CloakBrowser         : {cb_version}")
        print(f"扩展来源             : {source}")
        print(f"扩展 ID              : {ext_id}")
        print(f"目标                 : {TARGET_URL}")

        sig = inspect.signature(launch_persistent_context)
        bridge = tcp_listening(NDM_HOST, NDM_PORT)
        print(f"NDM TCP bridge       : {'LISTENING' if bridge else 'NOT LISTENING'}")
        if not bridge:
            print("[WARN] NDM 桌面端没在监听 10007，先启动 NDM。")

        effective_args = list(CHROMIUM_ARGS)
        effective_args.append(f"--disable-extensions-except={ext_dir}")
        effective_args.append(f"--load-extension={ext_dir}")

        launch_kwargs = {"headless": HEADLESS, "accept_downloads": True}
        arg_param_used = None
        for key in ("args","chromium_args","extra_args","browser_args","launch_args"):
            if key in sig.parameters:
                launch_kwargs[key] = effective_args
                arg_param_used = key
                break
        if "extension_paths" in sig.parameters:
            launch_kwargs["extension_paths"] = [str(ext_dir)]
        print(f"Chromium args param  : {arg_param_used}")

        context = launch_persistent_context(str(profile_dir), **launch_kwargs)

        browser = getattr(context, "browser", None)
        vattr = getattr(browser, "version", None) if browser else None
        report["browser_version"] = vattr() if callable(vattr) else vattr
        print(f"Chromium version     : {report['browser_version']}")

        def bind(pg):
            pg.on("response", lambda r: _rec_resp(r, network_events))
            pg.on("requestfailed", lambda r: _rec_fail(r, network_events))
            pg.on("console", lambda m: _rec_console(m, console_errors))
            pg.on("pageerror", lambda e: page_errors.append(str(e)) if len(page_errors) < 100 else None)

        if context.pages:
            page = context.pages[0]
            for pg in context.pages: bind(pg)
        else:
            page = context.new_page(); bind(page)

        print()
        print("等待扩展 SW 就绪...")
        time.sleep(2)
        diag0 = wake_and_read(context, ext_id)
        print(f"SW diag (启动后): {diag0}")

        try: page.bring_to_front()
        except Exception: pass

        print()
        print("打开目标页面...")
        resp = page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=60000)
        if resp: print(f"HTTP status          : {resp.status}")
        print(f"URL                  : {page.url}")
        print("等待 15 秒让播放器和扩展工作...")
        time.sleep(15)

        diag1 = wake_and_read(context, ext_id)
        print(f"SW diag (15秒后): {diag1}")

        def live():
            pages = list(context.pages)
            if not pages: return context.new_page()
            normals = [p for p in pages if not p.url.startswith("chrome-extension://")]
            return normals[-1] if normals else pages[-1]

        try:
            page = live()
            probe = page.evaluate(MEDIA_PROBE_JS)
            report["media_probe"] = probe
        except Exception as e:
            print(f"[WARN] 媒体探测失败：{e}")
            probe = {"media": []}

        report.update({
            "sw_diag_initial": diag0,
            "sw_diag_after_15s": diag1,
            "network_events": network_events[-MAX_NETWORK_EVENTS:],
            "console_errors": console_errors[-100:],
            "page_errors": page_errors[-100:],
        })

        sw_findings = classify_sw(diag1)
        report["sw_findings"] = sw_findings

        REPORT_FILE.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

        print()
        print("=" * 78)
        print("媒体探测")
        print("=" * 78)
        for m in probe.get("media", []):
            print(f"  [{m.get('index')}] {m.get('tag')} src={m.get('src','')[:120]}")

        print()
        print("=" * 78)
        print("扩展 / NDM 诊断")
        print("=" * 78)
        for line in sw_findings:
            print(line)

        print()
        print(f"完整报告: {REPORT_FILE.resolve()}")
        print("按回车键关闭（关闭后会彻底删除所有临时文件）。")
        input()
        return 0

    except Exception as exc:
        report["fatal_error"] = {"type": type(exc).__name__, "message": str(exc)}
        try: REPORT_FILE.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception: pass
        print(f"[FATAL] {type(exc).__name__}: {exc}")
        return 1

    finally:
        # --- 关闭浏览器 ---
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
            # 给 Chromium 一点时间真正退出，释放文件锁
            time.sleep(1.5)

        # --- 彻底删除临时目录 ---
        print()
        print("[CLEANUP] 正在删除临时目录...")
        cleanup_all(verbose=True)

        # 二次保险：如果 USE_TEMP_WORKSPACE 是 False，扩展目录仍要删
        # （make_temp_dir 已经注册了，所以 cleanup_all 会处理）


def _rec_resp(r, out):
    if len(out) >= MAX_NETWORK_EVENTS: return
    try:
        req = r.request; ct = r.headers.get("content-type",""); url = r.url
        if req.resource_type == "media" or ct.lower().startswith(("video/","audio/")) or ".m3u8" in url.lower() or ".mp4" in url.lower():
            out.append({"kind":"response","status":r.status,"resource_type":req.resource_type,"content_type":ct,"url":url})
    except Exception: pass


def _rec_fail(r, out):
    if len(out) >= MAX_NETWORK_EVENTS: return
    try:
        if r.resource_type == "media" or ".m3u8" in r.url.lower() or ".mp4" in r.url.lower():
            out.append({"kind":"requestfailed","resource_type":r.resource_type,"url":r.url,"failure":r.failure})
    except Exception: pass


def _rec_console(m, out):
    if m.type in ("error","warning") and len(out) < 100:
        try: out.append(f"{m.type}: {m.text}")
        except Exception: pass


if __name__ == "__main__":
    raise SystemExit(main())
