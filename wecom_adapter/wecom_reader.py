"""Run inside Apple LLDB only. Export the eight approved favorite fields over a pipe."""

import json
import os
import signal

import lldb

PREFIX = 'extern "C" void *objc_getClass(const char *);\nextern "C" void *sel_registerName(const char *);\nextern "C" void objc_msgSend(void);\n'
EXPRESSION = r"""({
  void *(*s0)(void*,void*) = (void*(*)(void*,void*))objc_msgSend;
  void *(*s1)(void*,void*,void*) = (void*(*)(void*,void*,void*))objc_msgSend;
  signed char (*yes)(void*,void*,void*) = (signed char(*)(void*,void*,void*))objc_msgSend;
  unsigned long long (*num)(void*,void*) = (unsigned long long(*)(void*,void*))objc_msgSend;
  void (*set)(void*,void*,void*,void*) = (void(*)(void*,void*,void*,void*))objc_msgSend;
  void *kind = sel_registerName("isKindOfClass:");
  void *responds = sel_registerName("respondsToSelector:");
  void *strCls = objc_getClass("NSString");
  void *modelCls = objc_getClass("WEWEmotionInfo");
  void *serviceCls = objc_getClass("WEWCollectionService");
  void *arrayCls = objc_getClass("NSArray");
  void *managerCls = objc_getClass("WEWServiceManager");
  unsigned long long result = 1;
  void *list = 0;
  void *payload = 0;
  if (managerCls && strCls && modelCls && serviceCls && arrayCls &&
      yes(managerCls,responds,sel_registerName("defaultServiceManager"))) {
    void *manager = s0(managerCls,sel_registerName("defaultServiceManager"));
    if (manager && yes(manager,responds,sel_registerName("collectionService"))) {
      void *svc = s0(manager,sel_registerName("collectionService"));
      if (svc && yes(svc,kind,serviceCls) && yes(svc,responds,sel_registerName("allEmotionCollections")))
        list = s0(svc,sel_registerName("allEmotionCollections"));
    }
  }
  if (list && yes(list,kind,arrayCls)) {
    unsigned long long n = num(list,sel_registerName("count"));
    result = 2;
    if (n <= 1000) {
      void *rows = s0(objc_getClass("NSMutableArray"),sel_registerName("array"));
      const char *fields[] = {"collectionId","fileId","emoUrl","md5","size","width","height","type"};
      bool ok = true;
      for (unsigned long long i=0; i<n && ok; ++i) {
        void *obj = ((void*(*)(void*,void*,unsigned long long))objc_msgSend)(list,sel_registerName("objectAtIndex:"),i);
        if (!obj || !yes(obj,kind,modelCls)) { ok=false; result=3; break; }
        void *row = s0(objc_getClass("NSMutableDictionary"),sel_registerName("dictionary"));
        for (unsigned int j=0; j<8 && ok; ++j) {
          void *sel = sel_registerName(fields[j]);
          if (!yes(obj,responds,sel)) { ok=false; result=4; break; }
          void *val = 0;
          if (j==1 || j==2 || j==3) {
            val = s0(obj,sel);
            if (!val) val = s0(objc_getClass("NSNull"),sel_registerName("null"));
            else if (!yes(val,kind,strCls) || num(val,sel_registerName("length"))>8192) {
              ok=false; result=5; break;
            }
          } else {
            long long v;
            if (j==5 || j==6) v=((unsigned int(*)(void*,void*))objc_msgSend)(obj,sel);
            else v=((long long(*)(void*,void*))objc_msgSend)(obj,sel);
            val=((void*(*)(void*,void*,long long))objc_msgSend)(objc_getClass("NSNumber"),sel_registerName("numberWithLongLong:"),v);
          }
          void *key=((void*(*)(void*,void*,const char*))objc_msgSend)(strCls,sel_registerName("stringWithUTF8String:"),fields[j]);
          set(row,sel_registerName("setObject:forKey:"),val,key);
        }
        if (ok) s1(rows,sel_registerName("addObject:"),row);
      }
      if (ok) {
        payload=((void*(*)(void*,void*,void*,unsigned long long,void*))objc_msgSend)(
          objc_getClass("NSJSONSerialization"),sel_registerName("dataWithJSONObject:options:error:"),rows,0,0);
        if (payload && num(payload,sel_registerName("length"))<=16777216) {
          payload=s0(payload,sel_registerName("retain"));
          result=(unsigned long long)payload;
        } else result=6;
      }
    }
  }
  result;
})"""


def run(debugger, command, result, internal_dict):
    # No object descriptions, raw memory scan, keys, chat or contact getters.
    pid, fd = map(int, command.split())
    process = frame = None
    pointer = 0
    payload = {"error": "reader_failed"}
    opts = lldb.SBExpressionOptions()
    opts.SetLanguage(lldb.eLanguageTypeObjC_plus_plus)
    opts.SetTimeoutInMicroSeconds(2_000_000)
    opts.SetOneThreadTimeoutInMicroSeconds(2_000_000)
    opts.SetUnwindOnError(True)
    opts.SetTrapExceptions(True)
    opts.SetStopOthers(True)
    opts.SetTryAllThreads(False)
    opts.SetIgnoreBreakpoints(True)
    opts.SetSuppressPersistentResult(True)
    opts.SetFetchDynamicValue(lldb.eNoDynamicValues)
    opts.SetAutoApplyFixIts(False)
    opts.SetRetriesWithFixIts(0)
    opts.SetPrefix(PREFIX)

    def timeout(signum, stack):
        raise RuntimeError("reader_timeout")

    def evaluate(expression):
        value = frame.EvaluateExpression(expression, opts)
        if value.GetError().Fail():
            raise RuntimeError("expression_timeout")
        error = lldb.SBError()
        number = value.GetValueAsUnsigned(error, 0)
        if error.Fail():
            raise RuntimeError("reader_failed")
        return number

    signal.signal(signal.SIGALRM, timeout)
    signal.alarm(35)
    try:
        debugger.SetAsync(False)
        target = debugger.CreateTarget(
            "/Applications/企业微信.app/Contents/MacOS/企业微信"
        )
        error = lldb.SBError()
        process = target.AttachToProcessWithID(debugger.GetListener(), pid, error)
        if error.Fail():
            raise RuntimeError("attach_rejected")
        if process.GetState() != lldb.eStateStopped:
            raise RuntimeError("reader_failed")
        threads = [t for t in process if t.GetQueueName() == "com.apple.main-thread"]
        if len(threads) != 1:
            raise RuntimeError("reader_failed")
        process.SetSelectedThread(threads[0])
        frame = threads[0].GetFrameAtIndex(0)
        pointer = evaluate(EXPRESSION)
        if pointer < 4096:
            sentinel, pointer = pointer, 0
            raise RuntimeError(
                "collection_limit" if sentinel == 2 else "collection_unavailable"
            )
        n = evaluate(
            '((unsigned long long(*)(void*,void*))objc_msgSend)((void*)%d,sel_registerName("length"))'
            % pointer
        )
        addr = evaluate(
            '(unsigned long long)((void*(*)(void*,void*))objc_msgSend)((void*)%d,sel_registerName("bytes"))'
            % pointer
        )
        if not 0 < n <= 16777216 or not addr:
            raise RuntimeError("collection_limit")
        error = lldb.SBError()
        raw = process.ReadMemory(addr, n, error)
        if error.Fail() or len(raw) != n:
            raise RuntimeError("reader_failed")
        payload = {"rows": json.loads(raw)}
        raw = None
    except (Exception, KeyboardInterrupt) as exc:
        allowed = {
            "attach_rejected",
            "expression_timeout",
            "collection_limit",
            "collection_unavailable",
            "reader_timeout",
        }
        payload = {
            "error": (
                str(exc)
                if type(exc) is RuntimeError and str(exc) in allowed
                else "reader_failed"
            )
        }
    finally:
        signal.alarm(0)
        if pointer and frame and process and process.GetState() == lldb.eStateStopped:
            try:
                evaluate(
                    '({ ((void(*)(void*,void*))objc_msgSend)((void*)%d,sel_registerName("release")); (unsigned long long)0; })'
                    % pointer
                )
            except (Exception, KeyboardInterrupt):
                payload = {"error": "reader_cleanup_failed"}
        if (
            process
            and process.IsValid()
            and process.GetState()
            not in (lldb.eStateInvalid, lldb.eStateDetached, lldb.eStateExited)
        ):
            error = process.Detach(False)
            if error.Fail() or process.GetState() != lldb.eStateDetached:
                payload = {"error": "detach_unconfirmed"}
        # Send data only after detach; no URLs on stdout, disk, argv or environment.
        with os.fdopen(fd, "wb") as stream:
            stream.write(json.dumps(payload).encode())


def __lldb_init_module(debugger, internal_dict):
    debugger.HandleCommand("command script add -f wecom_reader.run wecom-export")
