import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import { useI18n } from "../lib/i18n";
import { formatDate } from "../lib/money";
import { Button, Card, ErrorBox, Loading } from "./ui";
import { Icon } from "./Icon";

/**
 * Link Bale chats to this organization through the shared FinOps agent bot. Off until an admin asks for a code;
 * the installation then answers the bot's commands and sends alerts and the monthly report to linked chats.
 */
export function BaleCard() {
  const { t, lang } = useI18n();
  const qc = useQueryClient();
  const status = useQuery({ queryKey: ["bale"], queryFn: api.bale });
  const code = useMutation({ mutationFn: api.baleCode, onSuccess: () => qc.invalidateQueries({ queryKey: ["bale"] }) });
  const unlink = useMutation({ mutationFn: (id: number) => api.baleUnlink(id), onSuccess: () => qc.invalidateQueries({ queryKey: ["bale"] }) });
  const test = useMutation({ mutationFn: api.baleTest });
  if (status.isLoading) return <Loading />;
  if (status.error) return <ErrorBox error={status.error} onRetry={() => status.refetch()} />;
  const s = status.data!;
  if (!s.available) return null;
  const bot = code.data?.bot_username ?? s.bot_username;
  return (
    <Card title={t("baleTitle")}>
      <div className="stack">
        <p className="muted small">{t("baleLead")}</p>
        {s.chats.length ? (
          <ul className="plain stacked">
            {s.chats.map((c) => (
              <li key={c.chat_id} className="row between gap wrap">
                <span>
                  <Icon name="chat" small /> <bdi>{c.who}</bdi> <small className="muted">· {formatDate(new Date(c.linked_at * 1000).toISOString(), lang)}</small>
                </span>
                <Button variant="ghost" busy={unlink.isPending && unlink.variables === c.chat_id} onClick={() => window.confirm(t("baleUnlinkConfirm")) && unlink.mutate(c.chat_id)}>
                  {t("baleUnlink")}
                </Button>
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted">{t("baleNone")}</p>
        )}
        {code.data ? (
          <div className="alert alert-info stacked" role="status">
            <span>{t("baleSend").replace("{bot}", bot ? `@${bot}` : t("baleTheBot"))}</span>
            <code className="pre bale-code" dir="ltr">
              /start {code.data.code}
            </code>
            <small className="muted">{t("baleCodeExpires")}</small>
            {bot ? (
              <a href={`https://ble.ir/${bot}`} target="_blank" rel="noreferrer">
                <Icon name="external" small /> ble.ir/{bot}
              </a>
            ) : null}
          </div>
        ) : null}
        <div className="row gap wrap">
          <Button variant="primary" busy={code.isPending} onClick={() => code.mutate()}>
            <Icon name="link" /> {t("baleGetCode")}
          </Button>
          {s.chats.length ? (
            <Button busy={test.isPending} onClick={() => test.mutate()}>
              <Icon name="file" /> {t("baleTest")}
            </Button>
          ) : null}
        </div>
        {test.data ? <small className="muted">{t("baleTestSent")}</small> : null}
        <ErrorBox error={code.error ?? unlink.error ?? test.error} />
      </div>
    </Card>
  );
}
