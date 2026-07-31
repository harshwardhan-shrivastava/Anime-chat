// ===============================
// ELEMENTS
// ===============================

const chatBox = document.getElementById("chatBox");
const input = document.getElementById("messageInput");
const sendBtn = document.getElementById("sendBtn");

// ===============================
// SEND MESSAGE
// ===============================

function sendMessage() {

    const text = input.value.trim();

    if (text === "") return;

    // Remove welcome message

    const welcome = document.querySelector(".welcome-box");

    if (welcome) {

        welcome.remove();

    }

    // Create message

    const message = document.createElement("div");

    message.className = "message";

    message.innerHTML = `

        <div class="avatar">

            A

        </div>

        <div class="message-content">

            <h4>

                Anonymous

            </h4>

            <p>

                ${text}

            </p>

        </div>

    `;

    chatBox.appendChild(message);

    // Auto Scroll

    chatBox.scrollTop = chatBox.scrollHeight;

    // Clear input

    input.value = "";

}

// ===============================
// BUTTON CLICK
// ===============================

sendBtn.addEventListener("click", sendMessage);

// ===============================
// ENTER KEY
// ===============================

input.addEventListener("keypress", function(event){

    if(event.key === "Enter"){

        sendMessage();

    }

});