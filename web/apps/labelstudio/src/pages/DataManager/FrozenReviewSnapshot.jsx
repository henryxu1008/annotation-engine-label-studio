import { Typography } from "@humansignal/ui";
import { useLocaleTranslation } from "@humansignal/i18n";

/** Shared immutable source presentation; consumes a caller-authorized projection. */
export const FrozenReviewSnapshot = ({ snapshot }) => {
  const { t } = useLocaleTranslation("collaboration");
  return (
    <div>
      <Typography variant="body" size="medium">
        {t("immutableSnapshot")}
      </Typography>
      <pre data-testid="review-result-snapshot">{JSON.stringify(snapshot, null, 2)}</pre>
    </div>
  );
};
