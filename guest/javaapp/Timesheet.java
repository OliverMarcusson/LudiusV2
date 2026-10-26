// A small Swing app for the observer's Java Access Bridge path: the controls an office Java client typically has.
// Repo-owned test fixture, no third-party code. Run with a JDK 11+: java Timesheet.java
import java.awt.BorderLayout;
import java.awt.GridLayout;
import javax.swing.*;

public class Timesheet {
    public static void main(String[] args) {
        SwingUtilities.invokeLater(() -> {
            var frame = new JFrame("Timesheet");
            var menus = new JMenuBar();
            var file = new JMenu("File");
            file.add(new JMenuItem("New entry"));
            file.add(new JMenuItem("Save"));
            file.add(new JMenuItem("Exit"));
            menus.add(file);
            menus.add(new JMenu("Help"));
            frame.setJMenuBar(menus);

            var form = new JPanel(new GridLayout(0, 2, 8, 8));
            var employee = new JTextField("Anna Berg");
            var employeeLabel = new JLabel("Employee");
            employeeLabel.setLabelFor(employee);
            form.add(employeeLabel);
            form.add(employee);
            var pin = new JPasswordField("4711");
            var pinLabel = new JLabel("PIN");
            pinLabel.setLabelFor(pin);
            form.add(pinLabel);
            form.add(pin);
            form.add(new JLabel("Project"));
            form.add(new JComboBox<>(new String[] {"Internal", "Customer support", "Migration"}));
            form.add(new JCheckBox("Billable", true));

            var days = new String[60];
            for (int i = 0; i < days.length; i++) days[i] = String.format("Week %d: 40 h", i + 1);
            var list = new JList<>(days);
            list.setVisibleRowCount(8);

            var buttons = new JPanel();
            buttons.add(new JButton("Save"));
            buttons.add(new JButton("Cancel"));
            buttons.add(new JButton("Delete entry"));

            frame.add(form, BorderLayout.NORTH);
            frame.add(new JScrollPane(list), BorderLayout.CENTER);
            frame.add(buttons, BorderLayout.SOUTH);
            frame.setDefaultCloseOperation(WindowConstants.EXIT_ON_CLOSE);
            frame.setSize(520, 480);
            frame.setLocation(200, 150);
            frame.setVisible(true);
            frame.toFront();
        });
    }
}
