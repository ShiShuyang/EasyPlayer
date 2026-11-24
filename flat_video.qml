import QtQuick
import QtMultimedia

Rectangle {
    id: root
    color: "black"
    readonly property alias videoSink: output.videoSink
    property string volumeText: ""
    property string speedText: ""

    VideoOutput {
        id: output
        anchors.fill: parent
        fillMode: VideoOutput.PreserveAspectFit
    }

    component Badge: Rectangle {
        property alias text: label.text
        visible: label.text.length > 0
        implicitWidth: label.implicitWidth + 18
        implicitHeight: label.implicitHeight + 8
        radius: 5
        color: "#20252c"

        Text {
            id: label
            anchors.centerIn: parent
            color: "white"
            textFormat: Text.PlainText
        }
    }

    Badge {
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.margins: 16
        text: root.volumeText
    }

    Badge {
        anchors.top: parent.top
        anchors.right: parent.right
        anchors.margins: 16
        text: root.speedText
    }
}
