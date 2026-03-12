import { useTranslation } from "react-i18next";
import { I18nKey } from "#/i18n/declaration";
import { SettingsInput } from "../settings-input";
import { GitCodeTokenHelpAnchor } from "./gitcode-token-help-anchor";
import { KeyStatusIcon } from "../key-status-icon";
import { cn } from "#/utils/utils";

interface GitCodeTokenInputProps {
  onChange: (value: string) => void;
  onGitCodeHostChange: (value: string) => void;
  isGitCodeTokenSet: boolean;
  name: string;
  gitcodeHostSet: string | null | undefined;
  className?: string;
}

export function GitCodeTokenInput({
  onChange,
  onGitCodeHostChange,
  isGitCodeTokenSet,
  name,
  gitcodeHostSet,
  className,
}: GitCodeTokenInputProps) {
  const { t } = useTranslation();

  return (
    <div className={cn("flex flex-col gap-6", className)}>
      <SettingsInput
        testId={name}
        name={name}
        onChange={onChange}
        label={t(I18nKey.GITCODE$TOKEN_LABEL)}
        type="password"
        className="w-full max-w-[680px]"
        placeholder={isGitCodeTokenSet ? "<hidden>" : ""}
        startContent={
          isGitCodeTokenSet && (
            <KeyStatusIcon
              testId="gc-set-token-indicator"
              isSet={isGitCodeTokenSet}
            />
          )
        }
      />

      <SettingsInput
        onChange={onGitCodeHostChange || (() => {})}
        name="gitcode-host-input"
        testId="gitcode-host-input"
        label={t(I18nKey.GITCODE$HOST_LABEL)}
        type="text"
        className="w-full max-w-[680px]"
        placeholder="gitcode.com"
        defaultValue={gitcodeHostSet || undefined}
        startContent={
          gitcodeHostSet &&
          gitcodeHostSet.trim() !== "" && (
            <KeyStatusIcon testId="gc-set-host-indicator" isSet />
          )
        }
      />

      <GitCodeTokenHelpAnchor />
    </div>
  );
}
