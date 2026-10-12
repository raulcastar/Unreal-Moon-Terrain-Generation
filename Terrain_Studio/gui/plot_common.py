"""Drawing helpers shared by the studio's views. No Tk."""


def draw_message(figure, text):
    """A centred message in place of a plot (loading, failures, nothing yet)."""
    figure.clear()
    axes = figure.add_subplot(111)
    axes.axis("off")
    axes.text(0.5, 0.5, text, ha="center", va="center", fontsize=12,
              transform=axes.transAxes)
