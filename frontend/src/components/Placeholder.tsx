interface Props {
  title: string;
  phase: string;
  text: string;
}

/** Empty panel reserved for a later phase (texts come translated from the caller). */
export default function Placeholder({ title, phase, text }: Props) {
  return (
    <section className="panel placeholder">
      <div className="panel-head">
        <h2>{title}</h2>
        <span className="muted small">{phase}</span>
      </div>
      <div className="muted">{text}</div>
    </section>
  );
}
