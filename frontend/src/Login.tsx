import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { ApiError, authApi, type AuthStatus } from "./api";
import { useT } from "./i18n";

const Ctx = createContext<{ status: AuthStatus | null; logout: () => void }>({ status: null, logout: () => undefined });

/** Login status for the rest of the screen (phone? phone orders on? market blocked?). */
export function useAuth() {
  return useContext(Ctx);
}

/** Phase 5: the dashboard opens only after the login passcode (every device). */
export function LoginGate({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setStatus(await authApi.status());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void load();
    const again = () => void load();
    window.addEventListener("desk-login", again);       // any API call answered "login required"
    return () => window.removeEventListener("desk-login", again);
  }, [load]);

  const logout = useCallback(() => {
    void authApi.logout().finally(load);
  }, [load]);

  if (!status) return <Boot error={error} retry={load} />;
  if (status.required && !status.logged_in) return <Login status={status} onDone={load} />;
  return <Ctx.Provider value={{ status, logout }}>{children}</Ctx.Provider>;
}

function Boot({ error, retry }: { error: string | null; retry: () => void }) {
  const { t } = useT();
  useEffect(() => {
    if (!error) return;
    const id = window.setTimeout(retry, 5000);
    return () => window.clearTimeout(id);
  }, [error, retry]);
  return (
    <div className="boot">
      {error ? (
        <>
          <h1>{t("app.noBackend")}</h1>
          <p className="muted">{t("app.startHint")}</p>
        </>
      ) : <p className="muted">{t("app.loading")}</p>}
    </div>
  );
}

function Login({ status, onDone }: { status: AuthStatus; onDone: () => void }) {
  const { t } = useT();
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  const submit = async () => {
    setBusy(true);
    setMsg(null);
    try {
      await authApi.login(code);
      setCode("");
      onDone();
    } catch (e) {
      const b = (e as ApiError & { body?: { code?: string; attempts_left?: number; minutes?: number } }).body;
      if (b?.code === "wrong_passcode") setMsg(t("login.wrong", { n: b.attempts_left ?? 0 }));
      else if (b?.code === "locked") setMsg(t("login.locked", { m: b.minutes ?? 15 }));
      else if (b?.code === "passcode_not_set") setMsg(t("login.notSet"));
      else setMsg(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login">
      <h1>{t("login.title")}</h1>
      {!status.passcode_set ? (
        <p>{t("login.notSet")}</p>
      ) : (
        <form onSubmit={(e) => { e.preventDefault(); void submit(); }}>
          <input type="password" autoComplete="current-password" autoFocus value={code}
            onChange={(e) => setCode(e.target.value)} placeholder={t("login.placeholder")} />
          <button type="submit" disabled={busy || !code}>{t("login.button")}</button>
        </form>
      )}
      {msg && <p className="st-error">{msg}</p>}
      <p className="muted small">{status.remote ? t("login.remoteNote", { h: status.remote_hours ?? 24 }) : t("login.localNote", { d: status.local_days ?? 30 })}</p>
    </div>
  );
}
